"""
Revision Agent：一次性提出本轮**全部**修改补丁。

输入：

    当前论文（带段落 ID） + 需要解决的问题 + 用户约束 + 已被拒绝的改动

输出：

    patches[]：每条都带段落 ID、原文引用、修改后文本、修改理由

注意这里**不改文档**——改不改由用户逐条决定（补丁审阅闸门），
应用由 workflow 的 apply 节点统一完成。这样做的好处：

1. 一次产出全部改动，用户一眼看完全貌再一次勾选（不用来回确认好几轮）；
2. 每条改动可以单独接受/拒绝，理由由 Agent 写好，用户不需要打字；
3. 没被接受的改动永远不落盘，原文不会被悄悄改掉。
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any, Callable

from app.agents.prompts import REVISION_SYSTEM, render_patch_prompt
from app.config import get_settings
from app.document.patches import (
    STATUS_REJECTED,
    Patch,
    next_patch_id,
    patch_from_model,
    summarize_patches,
    validate_patch,
)
from app.document.spans import (
    Span,
    build_spans,
    chunk_spans,
    index_summary,
    render_annotated,
    resolve_span,
)
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.consistency import find_naming_variants
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.text import (
    looks_like_code,
    normalize_whitespace,
    similarity,
    truncate_middle,
)
from app.utils.tracing import utc_now


def run(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """提出本轮全部补丁，返回写回 State 的字段。"""

    log = logger or print
    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
    turn = int(state.get("turn", 1))
    document = state.get("current_document") or ""
    spans = current_spans(state)
    targets = collect_targets(state)

    if not document.strip() or not targets:
        reason = "论文内容为空。" if not document.strip() else "没有需要修改的问题。"
        log(f"[Revision] {reason}")
        return {
            "proposed_patches": [],
            "patch_stats": summarize_patches([]),
            "revision_summary": reason,
            "revision_history": [
                {
                    "iteration": iteration + 1,
                    "turn": turn,
                    "edits": 0,
                    "failed": 0,
                    "summary": reason,
                    "timestamp": utc_now(),
                    "applied": [],
                    "failed_edits": [],
                }
            ],
            "iteration": iteration + 1,
        }

    groups = _group_targets(spans, targets, settings.context.revision_max_tokens)
    log(
        f"[Revision] 本轮需要处理 {len(targets)} 个问题，"
        f"分 {len(groups)} 批提出补丁（一次性汇总给用户）。"
    )

    guard = get_guard("revision", on_event=log)
    counter = int(state.get("patch_counter", 0))
    issues_by_id = {issue.get("id"): issue for issue in targets}
    patches: list[Patch] = []
    notes: list[str] = []
    failed_batches = 0
    budget_stopped = False
    budget_snapshot: dict[str, Any] | None = None

    for batch_index, (chunk, chunk_issues) in enumerate(groups, start=1):
        try:
            check(state, agent="revision", stage=f"revision_batch_{batch_index}")
        except BudgetExceeded as exc:
            log(f"[Revision] 预算不足，停止后续批次：{exc.reason}")
            budget_stopped = True
            budget_snapshot = exceeded_snapshot(state, exc)
            break

        focus_ids = [issue.get("span_id") for issue in chunk_issues if issue.get("span_id")]
        chunk_text = render_annotated(chunk, with_sentences_for=focus_ids)
        fields = build_context(
            state,
            "revision",
            extra={
                "chunk_index": batch_index - 1,
                "chunk_total": len(groups),
                "chunk_text": chunk_text,
                "chunk_labels": [span.label for span in chunk],
                "issues": chunk_issues,
                # 前面批次已经提出的改动：后面的批次据此对齐术语/数字口径
                "round_patches": [patch.to_json() for patch in patches],
            },
        )
        prompt = render_patch_prompt(format_context(fields))

        try:
            response = guard.run(
                partial(
                    call_llm,
                    prompt,
                    "revision",
                    system=REVISION_SYSTEM,
                    json_mode=True,
                    run_id=run_id,
                    action=f"batch_{batch_index}",
                ),
                action=f"batch_{batch_index}",
                recorder=recorder,
                run_id=run_id,
                iteration=iteration,
                extra={"issues": [issue.get("id") for issue in chunk_issues]},
            )
            data = parse_json_object(response.text, required_keys=("patches",), context="Revision")
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其它批次
            failed_batches += 1
            log(f"[Revision] 第 {batch_index} 批提出补丁失败：{type(exc).__name__}: {exc}")
            continue

        note = str(data.get("notes") or "").strip()
        if note:
            notes.append(note)

        for item in data.get("patches") or []:
            if not isinstance(item, dict):
                continue
            counter += 1
            issue_id = item.get("issue_id")
            patch = patch_from_model(
                item,
                spans=spans,
                patch_id=next_patch_id(counter),
                round_number=iteration + 1,
                issue=issues_by_id.get(issue_id),
            )
            # 模型没写 issue_id 时，按它引用的段落猜一个（便于报告里对上问题）
            if patch.issue_id is None and patch.span_id:
                for issue in chunk_issues:
                    if issue.get("span_id") == patch.span_id:
                        patch.issue_id = issue.get("id")
                        break
            validate_patch(patch, spans)
            patches.append(patch)

    # 用户在闸门里定了「全文统一为 X」的词级口径 → **由程序**把全文替换一遍。
    # 不走模型：位置要穷尽（这篇 40+ 处）、替换正确性可判定、且代码不许碰。
    for patch in _unification_patches(spans, state.get("user_rules")):
        counter += 1
        patch.patch_id = next_patch_id(counter)
        validate_patch(patch, spans)
        patches.append(patch)

    patches = _dedupe(patches)
    patches = _drop_code_patches(patches, spans, notes, log)
    patches = _drop_noop_patches(patches, notes, log)
    patches = _mark_previously_rejected(patches, state)
    stats = summarize_patches(patches)
    summary = _build_summary(targets, patches, notes, failed_batches, budget_stopped)

    log(
        f"[Revision] 提出 {len(patches)} 条补丁"
        f"（可接受 {stats.get('proposed', 0)} 条，无法定位 {stats.get('conflict', 0)} 条）。"
    )

    return {
        "proposed_patches": [patch.to_json() for patch in patches],
        "patch_counter": counter,
        "patch_stats": stats,
        "patch_decisions": {},
        "revision_summary": summary,
        "iteration": iteration + 1,
        "spans": index_summary(spans),
        "budget": budget_snapshot or usage(state),
        "revision_history": [
            {
                "iteration": iteration + 1,
                "turn": turn,
                "stage": "propose",
                "edits": stats.get("proposed", 0) + stats.get("conflict", 0),
                "failed": stats.get("conflict", 0),
                "summary": summary,
                "timestamp": utc_now(),
                "applied": [],
                "failed_edits": [
                    patch.to_json()
                    for patch in patches
                    if patch.status == "conflict"
                ],
            }
        ],
    }


def current_spans(state: Any) -> list[Span]:
    """取当前文档的 span（State 里只有精简索引，这里重建完整对象）。"""

    document = state.get("current_document") or ""
    version = int(state.get("document_version", 1))
    return build_spans(document, version=version)


def collect_targets(state: Any) -> list[dict[str, Any]]:
    """
    本轮要处理的问题。

    优先级：

    1. Review 上一轮给出的剩余问题（重规划时最有针对性）；
    2. Audit 发现、且还没有被应用过的问题（首轮）；
    3. 用户本轮明确提出的修改要求（Direct Revision / 多轮追问）。
    """

    remaining = ((state.get("review_result") or {}).get("remaining_issues")) or []
    if remaining:
        targets = []
        for position, issue in enumerate(remaining, start=1):
            item = dict(issue)
            item.setdefault("id", position)
            item.setdefault("source", "review")
            targets.append(item)
        return targets

    findings = state.get("audit_findings") or []
    # 已经应用过、或者已经被用户拒绝过的问题，不再自动重提
    handled_issue_ids = {
        patch.get("issue_id")
        for patch in (state.get("applied_patches") or []) + (state.get("rejected_patches") or [])
    }
    if findings:
        pending = [
            issue
            for issue in findings
            if issue.get("id") not in handled_issue_ids and not issue.get("ignored")
            # 建议类不生成补丁：原文没写错，只写进 Word 批注给作者看。
            and (issue.get("kind") or "fix") != "advice"
        ]
        if pending:
            return pending

    # 用户本轮明确说的话优先（Direct Revision / 多轮追问），
    # Orchestrator 的通用指令只作为补充，不能盖过用户的要求。
    request = (state.get("user_request") or "").strip()
    instruction = (state.get("next_instructions") or "").strip()
    if request or instruction:
        problem = request or instruction
        return [
            {
                "id": 1,
                "category": "语言表达",
                "severity": "medium",
                "location": "",
                "problem": problem,
                "evidence": "",
                "suggestion": instruction or problem,
                "source": "user_request",
            }
        ]

    return []


def _locate_span(spans: list[Span], issue: dict[str, Any]) -> Span | None:
    """给一个问题找到它对应的段落。"""

    span = resolve_span(
        spans,
        span_id=str(issue.get("span_id") or ""),
        label=str(issue.get("span_label") or issue.get("location") or ""),
    )
    if span is not None:
        return span

    evidence = (issue.get("evidence") or "").strip()
    if evidence:
        for candidate in spans:
            if evidence in candidate.text:
                return candidate

    hint = evidence or (issue.get("problem") or "")
    if not hint:
        return None

    best, score = None, 0.0
    for candidate in spans:
        current = similarity(candidate.text, hint)
        if current > score:
            best, score = candidate, current
    return best if score >= 0.35 else None


def _group_targets(
    spans: list[Span],
    targets: list[dict[str, Any]],
    max_tokens: int,
) -> list[tuple[list[Span], list[dict[str, Any]]]]:
    """
    按位置把问题分组，每批的文本量不超过模型预算。

    文档不超预算时整篇一次处理（补丁之间的一致性最好）。
    """

    if not spans:
        return []

    total_tokens = sum(span.tokens for span in spans)
    if total_tokens <= max_tokens:
        return [(spans, targets)]

    chunks = chunk_spans(spans, max_tokens=max(500, int(max_tokens * 0.6)))
    if not chunks:
        return [(spans, targets)]

    buckets: dict[int, list[dict[str, Any]]] = {}
    extras: dict[int, set[str]] = {}
    for issue in targets:
        span = _locate_span(spans, issue)
        index = 0
        if span is not None:
            for position, chunk in enumerate(chunks):
                if any(item.id == span.id for item in chunk):
                    index = position
                    break
        buckets.setdefault(index, []).append(issue)
        # 矛盾常常"报在 A、要改 B"（例如报在参数表 T10、要改正文 P183）。
        # 把另一处的段落也塞进这一批，否则模型看不到要改的那段正文，
        # 就会"知道有问题但提不出补丁"。
        for label in issue.get("other_labels") or []:
            other = _locate_span(spans, {"span_label": label})
            if other is not None:
                extras.setdefault(index, set()).add(other.id)
        # 一条问题可能带**多个标号**（同一个写法在全文档几十处都不一致）。
        # 全部并进同一批，否则模型只看得到其中一个，其余照样漏。
        for label in issue.get("span_labels") or []:
            other = _locate_span(spans, {"span_label": label})
            if other is not None:
                extras.setdefault(index, set()).add(other.id)

    groups: list[tuple[list[Span], list[dict[str, Any]]]] = []
    for index in sorted(buckets):
        chunk = list(chunks[index] if index < len(chunks) else chunks[-1])
        wanted = extras.get(index) or set()
        if wanted:
            have = {item.id for item in chunk}
            chunk.extend(
                span for span in spans if span.id in wanted and span.id not in have
            )
            chunk.sort(key=lambda item: item.index)
        groups.append((chunk, buckets[index]))
    return groups


_UNIFY_RE = re.compile(r"全文统一为\s*[「\"'“]?([^\s」\"'”，,。；;]+)")


def _unification_targets(rules: Any) -> list[str]:
    """从用户要求里取出「全文统一为 X」里的目标写法 X。"""

    targets: list[str] = []
    for rule in rules or []:
        if rule.get("kind") != "rule" or rule.get("status", "active") != "active":
            continue
        for match in _UNIFY_RE.finditer(str(rule.get("text") or "")):
            token = match.group(1).strip()
            if token and token not in targets:
                targets.append(token)
    return targets


def _unique_quote(text: str, start: int, end: int, token: str) -> str:
    """
    给某一次出现造一个"在段落里唯一"的引用片段。

    补丁要求 quote 在段落内唯一，否则会被判"出现多次、无法判断改哪一处"。
    同一个词在一段里出现两次时，就向两侧扩窗，直到这个片段唯一、
    且**只包含这一处**该词为止。
    """

    length = len(text)
    for pad in (0, 4, 8, 12, 20, 30, 50, 80, 120):
        left = max(0, start - pad)
        right = min(length, end + pad)
        piece = text[left:right]
        if text.count(piece) == 1 and piece.count(token) == 1:
            return piece
    return text


def _unification_patches(spans: list[Span], rules: Any) -> list[Patch]:
    """
    用户定了「全文统一为 X」之后，**由程序**把全文替换一遍。

    为什么不交给模型：

    - 位置要**穷尽**（这篇论文里 MoCo 有 40+ 处），模型在长文里列不全；
    - 词级替换的正确性**完全可判定**（就是字符串替换），不需要模型判断；
    - 模型会连代码里的常量名一起改（`MOCO_QUEUE_SIZE`），而**代码一律不碰**。

    所以：口径来自用户在闸门上的选择（agent 的判断），
    执行由程序完成（穷尽、确定、零成本）。
    """

    targets = _unification_targets(rules)
    if not targets:
        return []

    # 代码段一律不参与：常量名/路径里的大小写不归本文档管
    prose = [span for span in spans if not looks_like_code(span.text)]
    by_label = {span.label: span for span in prose}

    patches: list[Patch] = []
    for item in find_naming_variants(prose):
        variants = item.get("variants") or {}
        target = next((token for token in targets if token in variants), None)
        if target is None:
            continue
        for token, labels in variants.items():
            if token == target:
                continue
            for label in labels:
                span = by_label.get(label)
                if span is None:
                    continue
                for match in re.finditer(re.escape(token), span.text):
                    quote = _unique_quote(
                        span.text, match.start(), match.end(), token
                    )
                    replaced = quote.replace(token, target)
                    if replaced == quote:
                        continue
                    patches.append(
                        Patch(
                            patch_id="",
                            span_id=span.id,
                            span_label=span.label,
                            target="sentence",
                            quote=quote,
                            replace=replaced,
                            rationale=(
                                f"按用户要求，全文统一为「{target}」；"
                                f"此处「{token}」改为「{target}」"
                            ),
                            category="专有名词",
                            severity="medium",
                            strategy="unification",
                        )
                    )
    return patches


def _drop_noop_patches(
    patches: list[Patch],
    notes: list[str],
    log: Callable[[str], None],
) -> list[Patch]:
    """
    丢掉"修改前后一模一样"的补丁。

    模型有时会提一条 replace 和 quote 逐字相同的补丁——等于什么都没改。
    它既不该出现在用户的清单里（清单里全是"要改什么"），
    更不该被显示成"**无法定位**"：那三个字会让人以为定位失败，
    而实际上压根没有改动可定位。这类补丁直接丢掉，只在日志里留一句计数。
    """

    kept = [patch for patch in patches if patch.conflict_code != "no_change"]
    dropped = len(patches) - len(kept)
    if dropped:
        note = f"已跳过 {dropped} 条「修改前后相同」的无效补丁"
        notes.append(note)
        log(f"[Revision] {note}")
    return kept


def _drop_code_patches(
    patches: list[Patch],
    spans: list[Span],
    notes: list[str],
    log: Callable[[str], None],
) -> list[Patch]:
    """
    丢掉所有落在**代码段**上的补丁。

    论文里的代码是"印刷出来的清单"，工具读到的是抽取文本：上下文往往不全
    （例如模块级常量根本没写进论文），它没有能力判断代码对不对。
    让它去"顺手修 bug"，结果是拿猜测去改正确的代码——
    而且代码段里有换行，改回 Word 也写不进去。所以代码一律不动。
    """

    by_id = {span.id: span for span in spans}
    by_label = {span.label: span for span in spans}

    kept: list[Patch] = []
    dropped_labels: set[str] = set()
    for patch in patches:
        span = by_id.get(getattr(patch, "span_id", "") or "") or by_label.get(
            getattr(patch, "span_label", "") or ""
        )
        if patch.conflict_code == "code_protected" or (
            span is not None and looks_like_code(span.text)
        ):
            if span.label:
                dropped_labels.add(span.label)
            continue
        kept.append(patch)

    dropped = len(patches) - len(kept)
    if dropped:
        where = "、".join(sorted(dropped_labels)) or "（未定位）"
        note = f"已跳过 {dropped} 条涉及代码段的改动（代码不由本工具修改）：{where}"
        notes.append(note)
        log(f"[Revision] {note}")
    return kept


def _dedupe(patches: list[Patch]) -> list[Patch]:
    """同一段落 + 相同替换内容的补丁只保留一条。"""

    seen: dict[tuple[str, str], Patch] = {}
    result: list[Patch] = []
    for patch in patches:
        key = (patch.span_id, normalize_whitespace(patch.replace))
        existing = seen.get(key)
        if existing is not None:
            existing.warnings.append(f"与 {patch.patch_id} 内容重复，已合并")
            continue
        seen[key] = patch
        result.append(patch)
    return result


def _mark_previously_rejected(patches: list[Patch], state: Any) -> list[Patch]:
    """
    与“已被拒绝的改动”比对：完全一样的改动直接标成已拒绝。

    这样用户不会被同一个方案反复打扰（多轮会话里的体验关键）。
    """

    memory = {
        (item.get("span_id", ""), normalize_whitespace(item.get("replace", "")))
        for item in (state.get("rejected_memory") or [])
    }
    if not memory:
        return patches

    for patch in patches:
        key = (patch.span_id, normalize_whitespace(patch.replace))
        if key in memory and patch.status == "proposed":
            patch.status = STATUS_REJECTED
            patch.decided_by = "system"
            patch.decision_note = "与之前被用户拒绝的改动相同，已自动跳过"
            patch.decided_at = utc_now()
    return patches


def _build_summary(
    targets: list[dict[str, Any]],
    patches: list[Patch],
    notes: list[str],
    failed_batches: int,
    budget_stopped: bool,
) -> str:
    """给 Review 与报告使用的摘要。"""

    stats = summarize_patches(patches)
    lines = [
        f"针对 {len(targets)} 个问题提出 {len(patches)} 条补丁："
        f"可接受 {stats.get('proposed', 0)} 条、"
        f"无法定位 {stats.get('conflict', 0)} 条、"
        f"历史已拒绝 {stats.get('rejected', 0)} 条。"
    ]
    for patch in patches[:6]:
        lines.append(f"- {patch.patch_id} {patch.summary}")
    if len(patches) > 6:
        lines.append(f"- ……（其余 {len(patches) - 6} 条见补丁清单）")
    if failed_batches:
        lines.append(f"- {failed_batches} 批补丁提出失败（已跳过）")
    if budget_stopped:
        lines.append("- 预算不足，未处理完全部问题")
    if notes:
        lines.append("- 模型说明：" + "；".join(truncate_middle(note, 120) for note in notes[:3]))
    return "\n".join(lines)


def all_applied_edits(state: Any) -> list[dict[str, Any]]:
    """汇总所有轮次真正应用的补丁（报告与界面用）。"""

    collected: list[dict[str, Any]] = []
    for entry in state.get("applied_patches") or []:
        collected.append(dict(entry))
    if collected:
        return collected
    # 兼容旧数据（没有补丁模型时用的 revision_edits）
    return list(state.get("revision_edits") or [])


def all_failed_edits(state: Any) -> list[dict[str, Any]]:
    """汇总所有无法定位的补丁。"""

    collected = [dict(entry) for entry in (state.get("conflict_patches") or [])]
    if collected:
        return collected
    return list(state.get("revision_failed_edits") or [])
