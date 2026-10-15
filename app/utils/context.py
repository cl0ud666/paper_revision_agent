"""
Context 治理（State → Context）。

核心思想：

    完整任务状态（State）
            ↓  Context Manager（本模块）
    当前 Agent 真正需要看到的信息
            ↓
          LLM

State ≠ Context：

- State 是任务的全部状态，可以很大；
- Context 是给一次 LLM 调用的输入，必须有预算。

本模块为每个 Agent 定义**字段白名单**（只给它需要的），
并对超预算的内容做“头尾保留 + 中间省略”的截断，
避免长论文把 Context Window 撑爆。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.config import get_settings
from app.document.spans import render_outline
from app.utils.budget import usage as budget_usage
from app.utils.json_utils import compact_json
from app.utils.rules import describe as describe_rules
from app.utils.text import document_stats, estimate_tokens, truncate_middle

# 每个 Agent 允许看到的字段（顺序即展示顺序）
AGENT_CONTEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "orchestrator": (
        "user_request",
        "user_rules",
        "conversation",
        "turn_info",
        "document_stats",
        "document_outline",
        "iteration",
        "review_result",
        "open_issues",
        "patch_stats",
        "last_plan",
        "budget_info",
    ),
    "audit": (
        "user_request",
        "user_rules",
        "ignored_issues",
        "focus_areas",
        "instructions",
        "document_stats",
        "chunk_position",
        "consistency_check",
        "name_list",
        "candidate_issues",
        "chunk_text",
    ),
    "revision": (
        "user_request",
        "user_rules",
        "instructions",
        "ignored_issues",
        "approved_issues",
        "rejected_memory",
        "round_patches",
        "chunk_position",
        "chunk_text",
    ),
    "review": (
        "user_request",
        "user_rules",
        "ignored_issues",
        "approved_issues",
        "patch_decisions",
        "revision_summary",
        "applied_edits",
        "failed_edits",
        "original_excerpt",
        "revised_excerpt",
    ),
}

# 字段中文名（拼进 Prompt 时更清楚）
FIELD_LABELS: dict[str, str] = {
    "user_request": "用户任务",
    "user_rules": "用户要求（跨轮次有效）",
    "ignored_issues": "用户明确不改的问题",
    "conversation": "之前的对话",
    "turn_info": "会话轮次",
    "document_stats": "论文规模",
    "document_outline": "论文结构（含段落 ID）",
    "iteration": "本轮修改循环",
    "review_result": "上一轮 Review 结果",
    "open_issues": "未通过的问题",
    "patch_stats": "补丁情况",
    "last_plan": "上一轮执行计划",
    "budget_info": "预算使用",
    "focus_areas": "重点关注方向",
    "instructions": "上游 Agent 的指令",
    "chunk_position": "当前片段位置",
    "consistency_check": "一致性核对提示（确定性检查）",
    "name_list": "专有名词清单（待核验写法）",
    "chunk_text": "待处理文本（带段落 ID）",
    "candidate_issues": "各分块报出的候选问题（待全局对齐）",
    "approved_issues": "需要解决的问题",
    "human_constraints": "用户约束（旧格式）",
    "rejected_memory": "已被用户拒绝的改动",
    "patch_decisions": "本轮补丁与用户决定",
    "revision_summary": "上一轮修改摘要",
    "applied_edits": "已应用的修改",
    "failed_edits": "未能落实的修改",
    "original_excerpt": "修改前原文",
    "revised_excerpt": "修改后文本",
}


# ---------- 各字段的构建函数 ----------


def _b_user_request(state: Any, extra: dict[str, Any]) -> str:
    request = str(state.get("user_request") or "（未提供）")
    turn = int(state.get("turn", 1))
    if turn > 1:
        return f"【第 {turn} 轮】{request}"
    return request


def _b_conversation(state: Any, extra: dict[str, Any]) -> str:
    """多轮会话：之前每轮用户说了什么、结果如何。"""

    turns = state.get("turns") or []
    if len(turns) <= 1:
        return ""
    lines: list[str] = []
    for entry in turns[:-1]:
        summary = entry.get("summary") or ""
        lines.append(
            f"- 第 {entry.get('turn')} 轮：用户要求「{entry.get('instruction', '')}」"
            f"；结果：{summary}"
        )
    return "\n".join(lines)


def _b_turn_info(state: Any, extra: dict[str, Any]) -> str:
    turn = int(state.get("turn", 1))
    iterations = int(state.get("iteration", 0))
    max_iterations = int(state.get("max_iterations", 3))
    return f"第 {turn} 轮会话；本轮已进行 {iterations}/{max_iterations} 次修改循环"


def _b_document_stats(state: Any, extra: dict[str, Any]) -> str:
    stats = document_stats(state.get("current_document") or "")
    spans = state.get("spans") or []
    if spans:
        stats += f"；切分为 {len(spans)} 个可寻址块"
    return stats


def _b_document_outline(state: Any, extra: dict[str, Any]) -> str:
    """优先用 span 索引（带段落 ID），没有就退回按标题解析。"""

    from app.document.spans import Span

    raw = state.get("spans") or []
    if raw:
        spans = [
            Span(
                id=item.get("id", ""),
                label=item.get("label", ""),
                kind=item.get("kind", "paragraph"),
                section=item.get("section", ""),
                text=item.get("preview", ""),
                start=int(item.get("start", 0)),
                end=int(item.get("end", 0)),
                index=0,
                version=int(item.get("version", 1)),
            )
            for item in raw
        ]
        return render_outline(spans, max_spans=int(extra.get("max_sections", 40)))

    from app.utils.text import document_outline as fallback_outline

    return fallback_outline(
        state.get("current_document") or "",
        max_sections=int(extra.get("max_sections", 24)),
        preview_chars=int(extra.get("preview_chars", 60)),
    )


def _b_iteration(state: Any, extra: dict[str, Any]) -> str:
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)
    return f"第 {iteration} 轮 / 最多 {max_iterations} 轮"


def _b_review_result(state: Any, extra: dict[str, Any]) -> str:
    result = state.get("review_result")
    if not result:
        return "（无，本次为首次规划）"
    compact = {
        "status": result.get("status"),
        "summary": result.get("summary", ""),
        "remaining_issues": result.get("remaining_issues", [])[:8],
        "constraint_violations": result.get("constraint_violations", [])[:5],
    }
    return compact_json(compact, max_chars=1800)


def _b_open_issues(state: Any, extra: dict[str, Any]) -> str:
    issues = state.get("audit_findings") or []
    approved = set(state.get("approved_issue_ids") or [])
    pending = [issue for issue in issues if issue.get("id") not in approved]
    if state.get("issue_gate"):
        return f"Audit 共发现 {len(issues)} 个问题，其中 {len(pending)} 个尚未被批准修改。"
    return f"Audit 共发现 {len(issues)} 个问题。"


def _b_patch_stats(state: Any, extra: dict[str, Any]) -> str:
    stats = state.get("patch_stats") or {}
    if not stats:
        return "（本轮还没有提出补丁）"
    parts = [
        f"{label} {stats.get(key, 0)} 条"
        for key, label in (
            ("proposed", "待决定"),
            ("accepted", "已接受"),
            ("rejected", "已拒绝"),
            ("applied", "已应用"),
            ("conflict", "定位失败"),
        )
        if stats.get(key)
    ]
    detail = "；".join(parts) if parts else "（无）"
    return f"本轮补丁：{detail}"


def _b_budget_info(state: Any, extra: dict[str, Any]) -> str:
    snapshot = state.get("budget") or {}
    if not snapshot:
        snapshot = budget_usage(state)
    max_run = snapshot.get("max_run_tokens", 0)
    used = snapshot.get("used_tokens", 0)
    remaining = max(0, int(max_run) - int(used)) if max_run else -1
    elapsed = snapshot.get("elapsed_seconds", 0)
    max_seconds = snapshot.get("max_run_seconds", 0)
    text = (
        f"已用 tokens {used}"
        + (f" / {max_run}（剩余约 {remaining}）" if max_run else "（未设上限）")
        + f"；已用时间 {elapsed}s"
        + (f" / {max_seconds:.0f}s" if max_seconds else "")
    )
    if snapshot.get("exceeded"):
        text += f"；已超出预算：{snapshot.get('reason')}"
    return text


def _b_last_plan(state: Any, extra: dict[str, Any]) -> str:
    plan = state.get("plan") or []
    reason = state.get("plan_reason") or ""
    if not plan and not reason:
        return "（无）"
    return "计划：" + "；".join(str(item) for item in plan) + (f"\n理由：{reason}" if reason else "")


def _b_focus_areas(state: Any, extra: dict[str, Any]) -> str:
    areas = extra.get("focus_areas") or state.get("focus_areas") or []
    return "、".join(str(area) for area in areas) if areas else "（未指定，按通用标准审查）"


def _b_instructions(state: Any, extra: dict[str, Any]) -> str:
    return str(extra.get("instructions") or state.get("next_instructions") or "（无）")


def _b_chunk_position(state: Any, extra: dict[str, Any]) -> str:
    index = extra.get("chunk_index")
    total = extra.get("chunk_total")
    labels = extra.get("chunk_labels") or []
    if index is None:
        return "（整篇文档）"
    text = f"第 {int(index) + 1} / {int(total)} 块"
    if labels:
        text += f"，包含段落 {labels[0]}–{labels[-1]}（共 {len(labels)} 个）"
    return text


def _b_chunk_text(state: Any, extra: dict[str, Any]) -> str:
    text = extra.get("chunk_text")
    if text is None:
        text = state.get("current_document") or ""
    return str(text)


def _b_consistency_check(state: Any, extra: dict[str, Any]) -> str:
    """
    一致性核对提示（确定性全文扫描的结果，不是模型猜的）。

    三类：

    1. 同一个专有名词在全文有几种写法（PubMed / Pubmed）；
    2. 只在摘要或结论出现、正文从未出现的方法名/数据集名（"实验没写 BERT、结论写了"）；
    3. 疑似拼错的专有名词（阿里云 ESC / ECS）。
    """

    hint = str(extra.get("consistency_check") or "").strip()
    if not hint:
        return ""
    return (
        "系统已对**全文**做过确定性扫描（不受分批评审的限制），结果如下，"
        "可以放心作为依据：\n"
        + hint
    )


def _b_name_list(state: Any, extra: dict[str, Any]) -> str:
    """专名核验用的清单（只在专名核验那一次调用里出现）。"""

    return str(extra.get("name_list") or "").strip()


def _b_candidate_issues(state: Any, extra: dict[str, Any]) -> str:
    """
    跨批对齐用的候选问题清单。

    Audit 的分块是并行跑的，各分块互相看不到对方的发现，同一个毛病因此
    容易被报好几遍。这里把**全部分块的发现**按分块顺序摆到一起，
    让模型做一次全局收敛（合并重复、指出互相矛盾的条目）。

    只在显式传入时出现：分块审查本身不带这个字段，所以不影响原有调用。
    """

    issues = extra.get("candidate_issues")
    if not issues:
        return ""

    lines: list[str] = []
    for position, issue in enumerate(issues, start=1):
        label = issue.get("span_label") or issue.get("location") or "?"
        category = issue.get("category") or ""
        severity = issue.get("severity") or ""
        problem = str(issue.get("problem") or "")[:80]
        lines.append(f"#{position} [{label}]（{category}/{severity}）{problem}")
        evidence = str(issue.get("evidence") or "")[:40]
        if evidence:
            lines.append(f"    证据：{evidence}")

    # 不在这里截断：候选清单要完整送进去，否则"全局对齐"会漏掉被砍掉的那些。
    # 长度由这一次调用的 Context 预算控制（PRA_AUDIT_CROSS_CHECK_TOKENS，0 = 不限）。
    text = "\n".join(lines)
    header = (
        "（这些是**全部分块**各自报出的候选问题。分块之间互相看不到，"
        "所以里面可能有重复、也可能有互相打架的条目。#1、#2… 是全量候选的序号）"
    )
    return f"{header}\n{text}"


def _b_approved_issues(state: Any, extra: dict[str, Any]) -> str:
    issues = extra.get("issues")
    if issues is None:
        approved = set(state.get("approved_issue_ids") or [])
        issues = [
            issue
            for issue in (state.get("audit_findings") or [])
            if not approved or issue.get("id") in approved
        ]
    compact = []
    for issue in issues:
        item = {
            "id": issue.get("id"),
            # 必须带上：Revision 要据此判断"这条是不是只有作者能拍板的矛盾"
            "kind": issue.get("kind") or "fix",
            "category": issue.get("category"),
            "severity": issue.get("severity"),
            "span_label": issue.get("span_label") or issue.get("location"),
            "location": issue.get("location"),
            "problem": issue.get("problem"),
            "evidence": issue.get("evidence"),
            "suggestion": issue.get("suggestion"),
        }
        compact.append({key: value for key, value in item.items() if value})
    if not compact:
        return "（无）"
    return json.dumps(compact, ensure_ascii=False, indent=1)


def _b_human_constraints(state: Any, extra: dict[str, Any]) -> str:
    """旧格式的约束（兼容没有 user_rules 的历史状态）。"""

    constraints = state.get("human_constraints") or []
    feedback = state.get("human_feedback") or ""
    lines = [f"- {constraint}" for constraint in constraints]
    if feedback:
        lines.append(f"- （补充说明）{feedback}")
    return "\n".join(lines) if lines else "（用户未额外限制）"


def _b_user_rules(state: Any, extra: dict[str, Any]) -> str:
    """
    用户说过的“点”都在这：带编号、可撤改、跨轮次有效。

    这是多轮会话里唯一需要长期记住的东西（不需要额外的 Memory 系统）。
    """

    rules = state.get("user_rules") or []
    text = describe_rules(rules, kinds=("rule",))
    legacy = _b_human_constraints(state, extra)
    if not text and legacy.startswith("- "):
        text = legacy
    if not text:
        return ""
    header = "（这些要求在所有轮次都有效；如要修改请指明编号，例如“R1 改成……”）"
    return f"{header}\n{text}"


def _b_ignored_issues(state: Any, extra: dict[str, Any]) -> str:
    """用户明确说过“不用改”的问题：不要再提、不要再问。"""

    rules = state.get("user_rules") or []
    text = describe_rules(rules, kinds=("ignore",))
    findings = [
        issue
        for issue in (state.get("audit_findings") or [])
        if issue.get("ignored")
    ]
    lines: list[str] = []
    if text:
        lines.append(text)
    if findings:
        lines.append(
            "本轮审查中命中以上决定、已自动忽略的问题："
            + "；".join(
                f"#{issue.get('id')} {issue.get('span_label') or ''} {issue.get('problem', '')[:30]}"
                for issue in findings
            )
        )
    return "\n".join(lines)


def _b_rejected_memory(state: Any, extra: dict[str, Any]) -> str:
    """用户拒绝过的改动：下一轮不要再提同样的方案。"""

    memory = state.get("rejected_memory") or []
    if not memory:
        return "（无）"
    lines: list[str] = []
    for item in memory[-10:]:
        note = f"（用户备注：{item['note']}）" if item.get("note") else ""
        lines.append(
            f"- 第 {item.get('round')} 轮 {item.get('span_label') or item.get('span_id')}："
            f"「{item.get('before', '')[:40]}」→「{item.get('replace', '')[:40]}」被拒绝{note}"
        )
    return "\n".join(lines)


def _b_round_patches(state: Any, extra: dict[str, Any]) -> str:
    """
    本轮**其它批次**已经提出的改动。

    分批修改时后面的批次看不到前面改了什么，容易出现
    "批 2 改成马铃薯、批 5 还写土豆"这类跨批次不一致。
    把已提出的改动摘成清单放进上下文，后面的批次就能主动对齐。
    """

    patches = extra.get("round_patches")
    if patches is None:
        patches = state.get("proposed_patches") or []
    if not patches:
        return ""

    lines: list[str] = []
    for patch in list(patches)[-25:]:
        before = str(patch.get("before") or patch.get("quote") or "整段")[:30]
        after = str(patch.get("replace") or "")[:30]
        lines.append(
            f"- {patch.get('span_label') or patch.get('span_id')}：「{before}」→「{after}」"
        )
    header = "（这些改动已经在本轮提出，请保持术语与数字口径一致，不要与它们冲突）"
    return f"{header}\n" + "\n".join(lines)


def _b_patch_decisions(state: Any, extra: dict[str, Any]) -> str:
    patches = extra.get("patches")
    if patches is None:
        patches = (state.get("proposed_patches") or []) + (state.get("applied_patches") or [])
    if not patches:
        return "（本轮没有补丁）"
    status_hint = {
        "applied": "已应用",
        "accepted": "已接受（待应用）",
        "rejected": "已被用户拒绝（不算未完成，不要因此判 fail）",
        "proposed": "待用户决定",
        "conflict": "未能落实（未改动原文，具体原因见补丁条目）",
    }
    lines: list[str] = []
    # 长文 + 多批次时补丁可能有几十条：这里不做"只看前 20 条"的截断，
    # 否则 Review 看不到全部改动，跨批次矛盾就无从判断（靠 token 预算整体裁剪）。
    max_items = int(extra.get("max_patches", 200))
    for patch in list(patches)[:max_items]:
        raw_status = str(patch.get("status") or "")
        # 优先用补丁自己的状态词：被拒的补丁按原因细分
        # （无法定位 / 定位有歧义 / 无效改动 / 改动冲突），不要再一律叫"无法定位"。
        status = (
            patch.get("status_label") or status_hint.get(raw_status) or raw_status
        )
        lines.append(
            f"- {patch.get('patch_id')} [{status}] "
            f"{patch.get('span_label') or patch.get('span_id')}："
            f"「{(patch.get('before') or patch.get('quote') or '整段')[:40]}」→"
            f"「{(patch.get('replace') or '')[:40]}」"
            f"｜理由：{(patch.get('rationale') or '—')[:60]}"
        )
    return "\n".join(lines)


def _b_revision_summary(state: Any, extra: dict[str, Any]) -> str:
    return str(state.get("revision_summary") or "（本轮还没有执行修改）")


def _b_applied_edits(state: Any, extra: dict[str, Any]) -> str:
    edits = extra.get("edits")
    if edits is None:
        edits = state.get("applied_patches") or state.get("revision_edits") or []
    compact = [
        {
            "issue_id": edit.get("issue_id"),
            "span": edit.get("span_label") or edit.get("span_id"),
            "original": edit.get("before") or edit.get("original"),
            "revised": edit.get("replace") or edit.get("revised"),
            "rationale": edit.get("rationale"),
        }
        for edit in edits
    ]
    return compact_json(compact, max_chars=2500) if compact else "（无）"


def _b_failed_edits(state: Any, extra: dict[str, Any]) -> str:
    edits = extra.get("failed_edits")
    if edits is None:
        edits = state.get("revision_failed_edits") or []
    if not edits:
        return "（无）"
    compact = [
        {
            "issue_id": edit.get("issue_id"),
            "span": edit.get("span_label") or edit.get("span_id"),
            "reason": edit.get("conflict_reason") or edit.get("reason"),
            "find": edit.get("quote") or edit.get("find"),
        }
        for edit in edits
    ]
    return compact_json(compact, max_chars=1200)


def _b_original_excerpt(state: Any, extra: dict[str, Any]) -> str:
    text = extra.get("original_excerpt")
    if text is None:
        return "（不适用）"
    return str(text)


def _b_revised_excerpt(state: Any, extra: dict[str, Any]) -> str:
    text = extra.get("revised_excerpt")
    if text is None:
        return "（不适用）"
    return str(text)


_BUILDERS: dict[str, Callable[[Any, dict[str, Any]], str]] = {
    "user_request": _b_user_request,
    "user_rules": _b_user_rules,
    "ignored_issues": _b_ignored_issues,
    "conversation": _b_conversation,
    "turn_info": _b_turn_info,
    "document_stats": _b_document_stats,
    "document_outline": _b_document_outline,
    "iteration": _b_iteration,
    "review_result": _b_review_result,
    "open_issues": _b_open_issues,
    "patch_stats": _b_patch_stats,
    "budget_info": _b_budget_info,
    "last_plan": _b_last_plan,
    "focus_areas": _b_focus_areas,
    "instructions": _b_instructions,
    "chunk_position": _b_chunk_position,
    "consistency_check": _b_consistency_check,
    "name_list": _b_name_list,
    "candidate_issues": _b_candidate_issues,
    "chunk_text": _b_chunk_text,
    "approved_issues": _b_approved_issues,
    "human_constraints": _b_human_constraints,
    "rejected_memory": _b_rejected_memory,
    "round_patches": _b_round_patches,
    "patch_decisions": _b_patch_decisions,
    "revision_summary": _b_revision_summary,
    "applied_edits": _b_applied_edits,
    "failed_edits": _b_failed_edits,
    "original_excerpt": _b_original_excerpt,
    "revised_excerpt": _b_revised_excerpt,
}

# 超预算时的裁剪顺序（越靠前越先被裁）
TRIM_ORDER = (
    "document_outline",
    "chunk_text",
    "conversation",
    "round_patches",
    "original_excerpt",
    "revised_excerpt",
    "applied_edits",
    "patch_stats",
    "last_plan",
    "review_result",
    "approved_issues",
    "rejected_memory",
    "ignored_issues",
    # 全局对齐的候选清单是那一次调用的**唯一**输入，
    # 放最后裁：宁可先砍其它背景信息。
    "candidate_issues",
)


def build_context(
    state: Any,
    agent: str,
    *,
    extra: dict[str, Any] | None = None,
    budget_tokens: int | None = None,
) -> dict[str, str]:
    """
    为某个 Agent 构建 Context。

    Returns:
        字段名 -> 文本块 的有序字典（只包含该 Agent 允许看到的字段）。
    """

    settings = get_settings()
    extra = extra or {}
    # 用 is None 判断：budget_tokens=0 表示"不限"，不能被 `or` 当成"没传"
    budget = (
        settings.context.budget_for(agent) if budget_tokens is None else budget_tokens
    )

    fields: dict[str, str] = {}
    for name in AGENT_CONTEXT_FIELDS.get(agent, ()):  # per-agent 白名单
        builder = _BUILDERS.get(name)
        if builder is None:
            continue
        value = builder(state, extra)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        fields[name] = str(value)

    return fit_budget(fields, budget, settings.context.prompt_head_ratio)


def fit_budget(
    fields: dict[str, str],
    budget_tokens: int,
    head_ratio: float = 0.6,
) -> dict[str, str]:
    """
    把 Context 压到预算以内。

    策略：按 TRIM_ORDER 依次缩减最大的文本块，最后再整段兜底截断。
    """

    if budget_tokens <= 0:
        return dict(fields)

    trimmed = dict(fields)
    total = sum(estimate_tokens(value) for value in trimmed.values())
    if total <= budget_tokens:
        return trimmed

    overshoot = total - budget_tokens
    for name in TRIM_ORDER:
        if overshoot <= 0:
            break
        if name not in trimmed:
            continue
        current = estimate_tokens(trimmed[name])
        target = max(120, current - overshoot)
        if target >= current:
            continue
        trimmed[name] = truncate_middle(trimmed[name], target, head_ratio=head_ratio)
        overshoot = sum(estimate_tokens(value) for value in trimmed.values()) - budget_tokens

    return trimmed


def format_context(fields: dict[str, str]) -> str:
    """把 Context 字段拼成给 LLM 的文本。"""

    blocks: list[str] = []
    for name, value in fields.items():
        label = FIELD_LABELS.get(name, name)
        blocks.append(f"## {label}\n{value}")
    return "\n\n".join(blocks)


def context_report(fields: dict[str, str]) -> dict[str, Any]:
    """Context 使用情况（便于观测与调试）。"""

    sizes = {name: estimate_tokens(value) for name, value in fields.items()}
    return {"fields": sizes, "total_tokens": sum(sizes.values())}
