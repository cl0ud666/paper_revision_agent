"""
Review Agent：独立复核（Revision 的对照面）。

职责非常明确：当前修改结果是否已经满足任务要求？输出 pass / fail。

它不修改论文，也不替 Orchestrator 做规划。

除了让 LLM 判断语义层面的满足度，这里还做了一层确定性检查：

- 修改后文本是否为空、是否明显变短（防止内容丢失）；
- 有目标问题却一处修改都没落实；
- 有哪些修改压根没定位成功（failed edits）。

这样即使模型判断偏松，明显的问题也不会被放行。
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable

from app.agents.prompts import REVIEW_SYSTEM, render_review_prompt
from app.config import get_settings
from app.utils.budget import BudgetExceeded
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.text import find_duplicate_paragraphs, similarity, truncate_middle
from app.utils.tracing import utc_now

DIMENSIONS = ("语法", "原意", "学术规范", "降重")

# 修改后长度低于原文这个比例，直接判定失败（内容丢失）
LENGTH_HARD_FLOOR = 0.6

# 低于这个比例只给警告
LENGTH_WARN_FLOOR = 0.8


def run(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """执行一次独立复核，返回写回 State 的字段。"""

    log = logger or print
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
    document = state.get("current_document") or ""
    original = state.get("original_document") or ""
    targets = _targets(state)

    checks, forced_remaining, hard_fail = _precheck(
        state, document=document, original=original, targets=targets
    )

    try:
        result = _llm_review(
            state,
            recorder=recorder,
            iteration=iteration,
            run_id=run_id,
            targets=targets,
            logger=log,
        )
    except BudgetExceeded as exc:
        # 预算不足时不做模型复核，只保留确定性检查的结论（并在报告里明确标注）
        log(f"[Review] 预算不足，跳过模型复核：{exc.reason}")
        forced_remaining = list(forced_remaining)
        soft_pass = not hard_fail and not forced_remaining
        result = {
            "status": "pass" if soft_pass else "fail",
            "summary": (
                f"预算不足，未执行模型复核：{exc.reason}。"
                "结论仅基于确定性检查（文档完整性、补丁落实情况、是否出现被拒改动）。"
            ),
            "per_issue": [],
            "remaining_issues": forced_remaining,
            "constraint_violations": [],
            "dimension_scores": {},
            "confidence": 0.0,
            "budget_exhausted": True,
        }
        checks.append({"name": "模型复核", "status": "skipped", "detail": exc.reason})
    except Exception as exc:  # noqa: BLE001 - 复核失败按未通过处理
        log(f"[Review] 复核失败（{type(exc).__name__}: {exc}），按未通过处理。")
        result = {
            "status": "fail",
            "summary": f"Review Agent 执行失败：{exc}",
            "per_issue": [],
            "remaining_issues": list(forced_remaining),
            "constraint_violations": [],
            "dimension_scores": {},
            "confidence": 0.0,
            "error": f"{type(exc).__name__}: {exc}",
        }
        hard_fail = True

    result["checks"] = checks

    # 用户拒绝 = 主动缩小范围：这些改动不算“未解决的问题”，
    # 否则多轮里会一直被判 fail、一直重复打扰用户。
    profiles = _rejected_profiles(state)
    ignored_profiles = _ignored_profiles(state)
    if profiles or ignored_profiles:
        remaining = result.get("remaining_issues") or []
        kept = [
            issue
            for issue in remaining
            if not _covered_by_rejected(issue, profiles)
            and not _covered_by_rejected(issue, ignored_profiles)
        ]
        dropped = len(remaining) - len(kept)
        result["remaining_issues"] = kept
        if dropped:
            checks.append(
                {
                    "name": "按用户决定缩小范围",
                    "status": "info",
                    "detail": (
                        f"{dropped} 条“剩余问题”属于用户已拒绝 / 已明确不改的范围，不计入未完成"
                    ),
                }
            )

        # 如果判 fail 的唯一理由是那些被拒绝的改动，且本轮工作本身没问题，就改判通过
        status_before = str(result.get("status") or "").lower()
        if (
            status_before == "fail"
            and not hard_fail
            and not kept
            and not (result.get("constraint_violations") or [])
            and state.get("rejected_patches")
            and state.get("applied_patches")
        ):
            result["status"] = "pass"
            result["summary"] = (
                f"{result.get('summary', '')}"
                "（除用户已拒绝的范围外，本轮提出的要求均已落实。）"
            ).strip()

    if forced_remaining:
        merged = list(result.get("remaining_issues") or [])
        existing_ids = {issue.get("id") for issue in merged}
        for issue in forced_remaining:
            if issue.get("id") not in existing_ids:
                merged.append(issue)
        result["remaining_issues"] = merged

    status = str(result.get("status") or "fail").lower()
    # 只要存在硬失败或“强制剩余问题”，即使模型自己说 pass 也不放行
    result["status"] = (
        "pass" if status == "pass" and not hard_fail and not forced_remaining else "fail"
    )

    log(
        f"[Review] 结论：{result['status'].upper()}，"
        f"剩余问题 {len(result.get('remaining_issues') or [])} 个。"
    )

    history_entry = {
        "iteration": iteration,
        "status": result["status"],
        "remaining": len(result.get("remaining_issues") or []),
        "summary": result.get("summary", ""),
        "timestamp": utc_now(),
    }

    update: dict[str, Any] = {
        "review_result": result,
        "review_history": [history_entry],
    }
    if result.get("error"):
        update["errors"] = [result["error"]]
    return update


def _mentions_rejected(item: dict[str, Any], rejected_ids: set[str]) -> bool:
    """判断复核意见是否只是在说“某个被拒绝的改动没做”。"""

    text = " ".join(str(item.get(key, "")) for key in ("reason", "id"))
    if any(patch_id in text for patch_id in rejected_ids):
        return True
    return "拒绝" in text and "未" in text


def _consistency_issues(state: Any, document: str) -> list[dict[str, Any]]:
    """
    跨批次一致性（确定性检查，不依赖模型）。

    "批 2 把土豆改成了马铃薯，批 5 那一段还写着土豆" —— 这类问题以前只能靠
    Review 那一次全局判断，而长文时它的视野会被预算截断。这里用补丁反推出的
    术语表（加上用户要求里的"把 A 写成 B"）全文扫一遍，命中就要求下一轮补上。
    """

    from app.document.spans import build_spans
    from app.utils.consistency import (
        find_inconsistencies,
        term_mappings_from_patches,
        term_mappings_from_rules,
    )

    mappings = term_mappings_from_rules(state.get("user_rules")) + term_mappings_from_patches(
        state.get("applied_patches")
    )
    if not mappings:
        return []

    fixed_spans = {
        str(patch.get("span_label"))
        for patch in (state.get("applied_patches") or [])
        if patch.get("span_label")
    }
    items = find_inconsistencies(
        document,
        mappings=mappings,
        spans=build_spans(document),
        fixed_spans=fixed_spans,
    )
    if not items:
        return []

    issues: list[dict[str, Any]] = []
    for index, item in enumerate(items[:3], start=1):
        spans = item.get("spans") or []
        issues.append(
            {
                "id": -100 - index,
                "category": "术语使用",
                "severity": "medium",
                "span_label": spans[0] if spans else "",
                "location": "、".join(spans[:6]) or "全文",
                "problem": (
                    f"术语/写法不一致：「{item['old']}」已在别处改为「{item['new']}」，"
                    f"但 {('、'.join(spans[:6]) or '全文')} 仍写着「{item['old']}」"
                    f"（全文共 {item['count']} 处）"
                ),
                "suggestion": f"统一改为「{item['new']}」",
                "source": "consistency",
            }
        )
    return issues


def _rejected_profiles(state: Any) -> list[dict[str, str]]:
    """用户拒绝过的改动对应的“位置 + 问题”，用于排除重复抱怨。"""

    findings = {
        issue.get("id"): issue for issue in (state.get("audit_findings") or [])
    }
    profiles: list[dict[str, str]] = []
    for patch in state.get("rejected_patches") or []:
        issue = findings.get(patch.get("issue_id")) or {}
        profiles.append(
            {
                "span_label": str(patch.get("span_label") or issue.get("span_label") or ""),
                "problem": str(issue.get("problem") or patch.get("rationale") or ""),
                "patch_id": str(patch.get("patch_id") or ""),
            }
        )
    return profiles


def _ignored_profiles(state: Any) -> list[dict[str, str]]:
    """用户明确说过“不用改”的问题，同样不计入未完成。"""

    profiles: list[dict[str, str]] = []
    for rule in state.get("user_rules") or []:
        if rule.get("kind") != "ignore" or rule.get("status", "active") != "active":
            continue
        signature = rule.get("signature") or {}
        profiles.append(
            {
                "span_label": str(signature.get("span_label") or ""),
                "problem": str(signature.get("problem") or rule.get("text") or ""),
                "patch_id": "",
            }
        )
    for issue in state.get("audit_findings") or []:
        if issue.get("ignored"):
            profiles.append(
                {
                    "span_label": str(issue.get("span_label") or ""),
                    "problem": str(issue.get("problem") or ""),
                    "patch_id": "",
                }
            )
    return profiles


def _covered_by_rejected(issue: dict[str, Any], profiles: list[dict[str, str]]) -> bool:
    """这条“剩余问题”是不是用户已经明确拒绝过的范围。"""

    label = str(issue.get("span_label") or "")
    problem = str(issue.get("problem") or "")
    for profile in profiles:
        if profile["patch_id"] and profile["patch_id"] == str(issue.get("patch_id") or ""):
            return True
        if label and profile["span_label"] and label == profile["span_label"]:
            return True
        if problem and profile["problem"] and similarity(problem, profile["problem"]) >= 0.6:
            return True
    return False


def _targets(state: Any) -> list[dict[str, Any]]:
    """
    本轮声称要解决的问题。

    优先用“本轮实际提交的补丁”反推，其次才是问题清单——
    因为现在改不改是由补丁决定的，问题清单只是输入。
    """

    patches = state.get("proposed_patches") or []
    if patches:
        ids = {patch.get("issue_id") for patch in patches if patch.get("issue_id")}
        findings = state.get("audit_findings") or []
        targets = [issue for issue in findings if issue.get("id") in ids]
        if targets:
            return targets

    approved_ids = set(state.get("approved_issue_ids") or [])
    findings = state.get("audit_findings") or []
    if approved_ids:
        targets = [issue for issue in findings if issue.get("id") in approved_ids]
        if targets:
            return targets

    remaining = ((state.get("review_result") or {}).get("remaining_issues")) or []
    return [issue for issue in remaining if not issue.get("ignored")]


def _precheck(
    state: Any,
    *,
    document: str,
    original: str,
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """确定性检查：返回 (检查项, 强制剩余问题, 是否直接失败)。"""

    checks: list[dict[str, Any]] = []
    forced_remaining: list[dict[str, Any]] = []
    hard_fail = False

    # 1) 文档不能为空
    if document.strip():
        checks.append(
            {"name": "文档非空", "status": "ok", "detail": f"{len(document)} 字符"}
        )
    else:
        checks.append({"name": "文档非空", "status": "fail", "detail": "修改后文档为空"})
        hard_fail = True

    # 2) 长度比例（防止大规模丢失内容）
    if original.strip():
        ratio = len(document) / len(original)
        if ratio < LENGTH_HARD_FLOOR:
            checks.append(
                {
                    "name": "内容完整度",
                    "status": "fail",
                    "detail": f"修改后长度仅为原文的 {ratio:.0%}，疑似丢失内容",
                }
            )
            hard_fail = True
            forced_remaining.append(
                {
                    "id": 0,
                    "category": "逻辑结构",
                    "severity": "high",
                    "location": "全文",
                    "problem": f"修改后文本长度仅为原文的 {ratio:.0%}，存在内容缺失风险",
                    "suggestion": "检查修改过程中是否丢失了段落或论证内容",
                    "source": "precheck",
                }
            )
        elif ratio < LENGTH_WARN_FLOOR:
            checks.append(
                {
                    "name": "内容完整度",
                    "status": "warn",
                    "detail": f"修改后长度为原文的 {ratio:.0%}",
                }
            )
        else:
            checks.append(
                {
                    "name": "内容完整度",
                    "status": "ok",
                    "detail": f"修改后长度为原文的 {ratio:.0%}",
                }
            )

    # 3) 有目标问题却没有任何修改落地
    applied = state.get("applied_patches") or state.get("revision_edits") or []
    if targets and not applied:
        checks.append(
            {
                "name": "修改是否落地",
                "status": "fail",
                "detail": f"本轮目标 {len(targets)} 个问题，但没有产生任何实际修改",
            }
        )
        hard_fail = True
    elif targets:
        checks.append(
            {
                "name": "修改是否落地",
                "status": "ok",
                "detail": f"目标 {len(targets)} 个问题，已落实 {len(applied)} 处修改",
            }
        )

    # 4) 未定位成功的修改
    failed_edits = state.get("revision_failed_edits") or []
    if failed_edits:
        checks.append(
            {
                "name": "修改定位",
                "status": "warn",
                "detail": f"{len(failed_edits)} 处修改未能定位到原文",
            }
        )
        failed_ids = {edit.get("issue_id") for edit in failed_edits}
        for issue in targets:
            if issue.get("id") in failed_ids:
                forced_remaining.append(
                    {
                        "id": issue.get("id"),
                        "category": issue.get("category", "语言表达"),
                        "severity": issue.get("severity", "medium"),
                        "location": issue.get("location", ""),
                        "problem": f"该问题本轮未能落实修改：{issue.get('problem', '')}",
                        "suggestion": issue.get("suggestion", ""),
                        "source": "precheck",
                    }
                )
    else:
        checks.append({"name": "修改定位", "status": "ok", "detail": "全部修改已定位"})

    # 5) 用户拒绝的改动不能被应用（尊重用户决定）
    rejected = state.get("rejected_patches") or []
    if rejected:
        document_text = state.get("current_document") or ""
        leaked = []
        for patch in rejected:
            needle = str(patch.get("replace") or "").strip()
            if len(needle) >= 8 and needle in document_text:
                leaked.append(patch.get("patch_id"))
        if leaked:
            checks.append(
                {
                    "name": "尊重用户决定",
                    "status": "fail",
                    "detail": f"被拒绝的补丁却出现在新文本中：{leaked}",
                }
            )
            hard_fail = True
            forced_remaining.append(
                {
                    "id": -9,
                    "category": "学术规范",
                    "severity": "high",
                    "location": "全文",
                    "problem": f"被用户拒绝的改动（{', '.join(str(x) for x in leaked)}）仍然出现在文本中",
                    "suggestion": "删除这些未获批准的改动",
                    "source": "precheck",
                }
            )
        else:
            checks.append(
                {
                    "name": "尊重用户决定",
                    "status": "ok",
                    "detail": f"{len(rejected)} 条被拒绝的补丁未出现在文本中",
                }
            )

    # 6) 跨批次一致性：已经统一成新写法的概念，是否还有地方写着旧写法
    consistency = _consistency_issues(state, document)
    if consistency:
        checks.append(
            {
                "name": "跨批次一致性",
                "status": "warn",
                "detail": f"发现 {len(consistency)} 处术语/写法不一致",
            }
        )
        forced_remaining.extend(consistency)
    else:
        checks.append({"name": "跨批次一致性", "status": "ok", "detail": "术语与写法一致"})

    # 7) 重复段落检测（常见于“替换整段”造成的复制粘贴式重复）
    duplicates = find_duplicate_paragraphs(document)
    if duplicates:
        checks.append(
            {
                "name": "段落重复",
                "status": "fail",
                "detail": f"检测到 {len(duplicates)} 段重复内容",
            }
        )
        for position, preview in enumerate(duplicates[:3], start=1):
            forced_remaining.append(
                {
                    "id": -position,
                    "category": "逻辑结构",
                    "severity": "high",
                    "location": "全文",
                    "problem": f"修改后出现整段重复内容：{preview}",
                    "suggestion": "删除重复段落，只保留一处完整表述",
                    "source": "precheck",
                }
            )
    else:
        checks.append({"name": "段落重复", "status": "ok", "detail": "未发现重复段落"})

    # 7) 约束存在性（信息性检查）
    constraints = state.get("human_constraints") or []
    checks.append(
        {
            "name": "用户约束",
            "status": "ok" if constraints else "info",
            "detail": f"{len(constraints)} 条约束" if constraints else "本次没有额外约束",
        }
    )

    return checks, forced_remaining, hard_fail


def _llm_review(
    state: Any,
    *,
    recorder: Any | None,
    iteration: int,
    run_id: str,
    targets: list[dict[str, Any]],
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """调用 Review Agent 做语义层面的独立判断。"""

    settings = get_settings()
    document = state.get("current_document") or ""
    applied = state.get("revision_edits") or []
    budget = settings.context.review_budget

    # 把“修改前 → 修改后”的证据整理出来给复核员看
    excerpt_budget = max(200, budget // 12)
    pairs: list[str] = []
    for edit in applied[:8]:
        pairs.append(
            "【修改前】"
            + truncate_middle(str(edit.get("original", "")), excerpt_budget)
            + "\n【修改后】"
            + truncate_middle(str(edit.get("revised", "")), excerpt_budget)
            + f"\n【对应问题】{edit.get('issue_id')} {edit.get('rationale', '')}"
        )

    fields = build_context(
        state,
        "review",
        extra={
            "issues": targets,
            "patches": (state.get("proposed_patches") or [])
            + (state.get("applied_patches") or [])
            + (state.get("rejected_patches") or []),
            "original_excerpt": "\n\n".join(pairs) if pairs else "（本轮没有产生修改）",
            # 这里传完整文档，交给 Context 预算统一裁剪；
            # 预先截断会让复核员看到“残缺文本”而误判内容缺失。
            "revised_excerpt": document,
        },
    )
    prompt = render_review_prompt(format_context(fields))

    guard = get_guard("review", on_event=logger)
    response = guard.run(
        partial(
            call_llm,
            prompt,
            "review",
            system=REVIEW_SYSTEM,
            json_mode=True,
            run_id=run_id,
            action="verify",
        ),
        action="verify",
        recorder=recorder,
        run_id=run_id,
        iteration=iteration,
        extra={"targets": [issue.get("id") for issue in targets]},
    )
    data = parse_json_object(response.text, required_keys=("status",), context="Review")
    return _normalize_review(data)


def _normalize_review(data: dict[str, Any]) -> dict[str, Any]:
    """规范化 Review Agent 的输出。"""

    status = str(data.get("status") or "").strip().lower()
    if status not in {"pass", "fail"}:
        status = "fail"

    remaining: list[dict[str, Any]] = []
    for position, issue in enumerate(data.get("remaining_issues") or [], start=1):
        if not isinstance(issue, dict):
            continue
        remaining.append(
            {
                "id": issue.get("id", position),
                "category": issue.get("category", "语言表达"),
                "severity": str(issue.get("severity", "medium")).lower(),
                "span_label": issue.get("span_label", ""),
                "location": issue.get("location", ""),
                "problem": issue.get("problem", ""),
                "suggestion": issue.get("suggestion", ""),
                "source": "review",
            }
        )

    per_issue: list[dict[str, Any]] = []
    for item in data.get("per_issue") or []:
        if isinstance(item, dict):
            per_issue.append(
                {
                    "id": item.get("id"),
                    "status": str(item.get("status", "")).lower(),
                    "reason": item.get("reason", ""),
                }
            )

    scores: dict[str, int] = {}
    raw_scores = data.get("dimension_scores") or {}
    if isinstance(raw_scores, dict):
        for dimension in DIMENSIONS:
            value = raw_scores.get(dimension)
            if value is None:
                continue
            try:
                scores[dimension] = int(float(value))
            except (TypeError, ValueError):
                continue

    violations = [
        str(item).strip()
        for item in (data.get("constraint_violations") or [])
        if str(item).strip()
    ]

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "status": status,
        "summary": str(data.get("summary") or "").strip(),
        "per_issue": per_issue,
        "remaining_issues": remaining,
        "constraint_violations": violations,
        "dimension_scores": scores,
        "confidence": max(0.0, min(1.0, confidence)),
    }
