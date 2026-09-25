"""
Audit Agent：系统性发现问题。

流程：

    论文 → 切成可寻址的块（span） → 按块分组 → 逐块审查 → 合并去重

与上一版的区别：**分块按段落边界切**（不是按字符切），
因此模型看到的每个块都带段落 ID（``[P03]``），
它给出的每条问题也都会带上这个 ID——后续 Revision 才能精确落补丁。

硬约束：evidence 必须逐字来自该段落，这样补丁校验能一次通过。
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable

from app.agents.prompts import AUDIT_SYSTEM, render_audit_prompt
from app.config import get_settings
from app.document.spans import (
    Span,
    build_spans,
    chunk_spans,
    index_summary,
    render_annotated,
    resolve_span,
    select_auditable,
    structure_report,
)
from app.state.schema import ISSUE_CATEGORIES, SEVERITY_ORDER
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.consistency import (
    describe_entity_check,
    describe_naming_variants,
    find_cross_section_gaps,
    find_naming_variants,
    find_near_miss_names,
)
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.rules import is_ignored
from app.utils.text import locate_section, normalize_whitespace, similarity

SEVERITIES = ("high", "medium", "low")
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _consistency_hint(
    chunk_labels: set[str],
    *,
    naming: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
) -> str:
    """
    只挑"与本批次有关"的一致性线索放进上下文。

    全量塞进去有两个坏处：占掉审查正文的预算；同一个词会被每批评一次。
    所以三类线索都按"是否出现在本批次"过滤。
    """

    def relevant(labels: list[str]) -> bool:
        return any(label in chunk_labels for label in labels)

    relevant_naming = [
        item
        for item in naming
        if relevant([label for labels in (item.get("variants") or {}).values() for label in labels])
    ]
    relevant_gaps = [item for item in gaps if relevant(list(item.get("labels") or []))]
    relevant_misses = [
        item
        for item in near_misses
        if relevant(list(item.get("suspected_labels") or []))
        or relevant(list(item.get("likely_labels") or []))
    ]

    sections: list[str] = []
    naming_text = describe_naming_variants(relevant_naming)
    if naming_text:
        sections.append(naming_text)
    sections.extend(describe_entity_check(relevant_gaps, relevant_misses))
    return "\n\n".join(sections)


# 证据必须能在段落里逐字找到，且不能太短（太短容易"碰巧命中"）
MIN_EVIDENCE_CHARS = 8

# 同类系统性问题聚合的相似度阈值
SYSTEMATIC_SIMILARITY = 0.6


def run(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """执行一次完整审查，返回写回 State 的字段。"""

    log = logger or print
    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
    document = state.get("current_document") or ""

    if not document.strip():
        log("[Audit] 论文内容为空，跳过审查。")
        return {
            "audit_findings": [],
            "audit_failed": False,
            "audit_meta": {"chunks": 0, "raw_issues": 0, "failed_chunks": 0},
            "spans": [],
        }

    version = int(state.get("document_version", 1))
    spans = build_spans(document, version=version)
    scope = state.get("audit_scope") or settings.workflow.audit_scope
    auditable = select_auditable(spans, scope) or spans
    # 先按段落切够，再按上限取：这样"因为太长而没审到"是可以被说出来的
    all_chunks = chunk_spans(
        auditable,
        max_tokens=settings.context.audit_chunk_tokens,
        max_chunks=None,
    )
    max_chunks = settings.context.audit_max_chunks
    chunks = all_chunks[:max_chunks] if max_chunks else all_chunks
    dropped_chunks = len(all_chunks) - len(chunks)

    # 确定性一致性检查（不吃"批次运气"）：写法不统一 / 只在总结里出现 / 疑似拼错
    naming_hint = find_naming_variants(auditable)
    section_gaps = find_cross_section_gaps(auditable)
    near_misses = find_near_miss_names(auditable)

    structure = structure_report(spans)
    log(
        f"[Audit] 文档共 {structure['blocks']} 块；按范围「{scope}」送审 "
        f"{len(auditable)} 块，分 {len(chunks)} 批"
        f"（跳过封面/目录/致谢等 {structure['blocks'] - len(auditable)} 块）。"
    )
    if dropped_chunks:
        log(
            f"[Audit] 注意：受上限 PRA_AUDIT_MAX_CHUNKS={max_chunks} 限制，"
            f"后面 {dropped_chunks} 批没有审到（整篇共 {len(all_chunks)} 批）。"
            "需要审全文请调大 PRA_AUDIT_MAX_CHUNKS 或 PRA_AUDIT_CHUNK_TOKENS。"
        )

    guard = get_guard("audit", on_event=log)
    raw_issues: list[dict[str, Any]] = []
    failed_chunks = 0
    budget_stopped = False
    budget_snapshot: dict[str, Any] | None = None

    for index, chunk in enumerate(chunks):
        try:
            check(state, agent="audit", stage=f"audit_chunk_{index + 1}")
        except BudgetExceeded as exc:
            log(f"[Audit] 预算不足，停止后续审查：{exc.reason}")
            budget_stopped = True
            budget_snapshot = exceeded_snapshot(state, exc)
            break

        chunk_text = render_annotated(chunk)
        chunk_labels = {span.label for span in chunk}
        consistency_hint = _consistency_hint(
            chunk_labels,
            naming=naming_hint,
            gaps=section_gaps,
            near_misses=near_misses,
        )
        fields = build_context(
            state,
            "audit",
            extra={
                "chunk_index": index,
                "chunk_total": len(chunks),
                "chunk_text": chunk_text,
                "chunk_labels": [span.label for span in chunk],
                "consistency_check": consistency_hint,
            },
        )
        prompt = render_audit_prompt(format_context(fields))

        try:
            response = guard.run(
                partial(
                    call_llm,
                    prompt,
                    "audit",
                    system=AUDIT_SYSTEM,
                    json_mode=True,
                    run_id=run_id,
                    action=f"chunk_{index + 1}",
                ),
                action=f"chunk_{index + 1}",
                recorder=recorder,
                run_id=run_id,
                iteration=iteration,
                extra={"chunk_index": index, "spans": len(chunk)},
            )
            data = parse_json_object(response.text, required_keys=("issues",), context="Audit")
        except Exception as exc:  # noqa: BLE001 - 单个片段失败不影响其它片段
            failed_chunks += 1
            log(f"[Audit] 第 {index + 1} 批审查失败：{type(exc).__name__}: {exc}")
            continue

        for item in data.get("issues") or []:
            normalized = _normalize_issue(item, chunk=chunk, document=document)
            if normalized is not None:
                raw_issues.append(normalized)

    start_id = int(state.get("issue_counter", 0)) + 1
    merged = _merge_issues(
        raw_issues, limit=settings.context.max_issues, start_id=start_id
    )
    # 逐字核对证据 + 合并同类系统性问题 + 按严重程度取舍
    spans_by_id = {span.id: span for span in spans}
    merged, screen_meta = _screen(
        merged,
        spans_by_id=spans_by_id,
        min_severity=settings.context.audit_min_severity,
        max_issues=settings.context.audit_max_issues,
    )
    next_issue_counter = start_id + len(merged) - 1 if merged else start_id - 1

    # 用户明确说过“不用改”的问题：标出来，不再进入人工确认与补丁阶段
    rules = state.get("user_rules") or []
    ignored_count = 0
    for issue in merged:
        if rules and is_ignored(issue, rules):
            issue["ignored"] = True
            ignored_count += 1
    audit_failed = bool(chunks) and failed_chunks == len(chunks) and not merged

    meta = {
        "chunks": len(chunks),
        "failed_chunks": failed_chunks,
        "raw_issues": len(raw_issues),
        "merged_issues": len(merged),
        "blocks": len(spans),
        "scope": scope,
        "auditable_blocks": len(auditable),
        "chunks_total": len(all_chunks),
        "chunks_dropped": dropped_chunks,
        "naming_variants": len(naming_hint),
        "zones": structure["zones"],
        "skipped_noise": structure["skipped_noise"],
        "budget_stopped": budget_stopped,
        "ignored_by_user": ignored_count,
        "dropped_unverifiable": screen_meta["unverifiable"],
        "dropped_low_severity": screen_meta["low_severity"],
        "merged_systematic": screen_meta["merged_systematic"],
        "high": sum(1 for issue in merged if issue["severity"] == "high"),
        "medium": sum(1 for issue in merged if issue["severity"] == "medium"),
        "low": sum(1 for issue in merged if issue["severity"] == "low"),
    }

    if audit_failed:
        log("[Audit] 所有批次的审查都失败了，标记为审查不可用。")
    else:
        located = sum(1 for issue in merged if issue.get("span_id"))
        note = f"，其中 {ignored_count} 个按用户要求自动忽略" if ignored_count else ""
        if screen_meta["unverifiable"] or screen_meta["low_severity"]:
            note += (
                f"（已丢弃无法逐字核对 {screen_meta['unverifiable']} 条、"
                f"低价值 {screen_meta['low_severity']} 条）"
            )
        log(
            f"[Audit] 审查完成，得到 {len(merged)} 个问题"
            f"（{located} 个已定位到段落）{note}。"
        )

    return {
        "audit_findings": merged,
        "audit_failed": audit_failed,
        "audit_meta": meta,
        "spans": index_summary(spans),
        "document_version": version,
        "issue_counter": max(int(state.get("issue_counter", 0)), next_issue_counter),
        "budget": budget_snapshot or usage(state),
    }


def _normalize_issue(
    item: Any,
    *,
    chunk: list[Span],
    document: str,
) -> dict[str, Any] | None:
    """规范化一条问题，并把段落标签解析成稳定 ID。"""

    if not isinstance(item, dict):
        return None

    problem = str(item.get("problem") or "").strip()
    if not problem:
        return None

    category = str(item.get("category") or "").strip()
    if category not in ISSUE_CATEGORIES:
        category = "语言表达"

    severity = str(item.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"

    evidence = str(item.get("evidence") or "").strip()
    label = str(item.get("span_label") or item.get("span_id") or "").strip()
    span = resolve_span(chunk, span_id=label, label=label)

    # 标签没写对时，退一步：按 evidence 找到它所在的块
    if span is None and evidence:
        for candidate in chunk:
            if evidence and evidence in candidate.text:
                span = candidate
                break
    if span is None and evidence:
        best, score = None, 0.0
        for candidate in chunk:
            current = similarity(candidate.text, evidence)
            if current > score:
                best, score = candidate, current
        if best is not None and score >= 0.6:
            span = best

    location = str(item.get("location") or "").strip()
    if not location:
        location = span.section if span is not None else locate_section(document, evidence)

    issue: dict[str, Any] = {
        "category": category,
        "severity": severity,
        "location": location,
        "problem": problem,
        "evidence": evidence,
        "suggestion": str(item.get("suggestion") or "").strip(),
        "span_id": span.id if span is not None else "",
        "span_label": span.label if span is not None else label,
        "source": "audit",
    }
    if span is None:
        issue["warning"] = "未能定位到具体段落"
    return issue


def _merge_issues(
    issues: list[dict[str, Any]], *, limit: int = 30, start_id: int = 1
) -> list[dict[str, Any]]:
    """合并跨批次的重复问题，按严重程度排序，并统一编号。"""

    merged: list[dict[str, Any]] = []
    for issue in issues:
        duplicate = None
        for existing in merged:
            if similarity(existing["problem"], issue["problem"]) >= 0.82:
                duplicate = existing
                break
            if (
                issue.get("evidence")
                and existing.get("evidence")
                and similarity(existing["evidence"], issue["evidence"]) >= 0.9
            ):
                duplicate = existing
                break

        if duplicate is None:
            merged.append(dict(issue))
            continue

        if SEVERITY_ORDER[issue["severity"]] < SEVERITY_ORDER[duplicate["severity"]]:
            duplicate["severity"] = issue["severity"]
        for key in ("suggestion", "evidence", "location", "span_id", "span_label"):
            if not duplicate.get(key) and issue.get(key):
                duplicate[key] = issue[key]

    merged.sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], item.get("span_id", "")))
    for position, issue in enumerate(merged[:limit], start=start_id):
        issue["id"] = position
        issue["approved"] = False
    return merged[:limit]


def _screen(
    issues: list[dict[str, Any]],
    *,
    spans_by_id: dict[str, Span],
    min_severity: str = "medium",
    max_issues: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    审查后处理（这一步直接决定"会不会给一篇好论文报一堆问题"）：

    1. **逐字核对证据**：evidence 在对应段落里找不到 → 丢弃（防幻觉/防错位）；
    2. **同类系统性问题聚合**：同分类且问题描述高度相似的多条 → 合并成一条，
       并列出涉及的所有段落（"参考文献格式不统一（共 12 处：P581、P582…）"）；
    3. **严重程度门槛**：默认丢掉 low（可改可不改）；
    4. **总量上限**：超过上限时按严重程度截断。
    """

    meta = {"unverifiable": 0, "low_severity": 0, "merged_systematic": 0}
    verified: list[dict[str, Any]] = []

    for issue in issues:
        span = spans_by_id.get(issue.get("span_id") or "")
        evidence = (issue.get("evidence") or "").strip()
        if span is None or len(evidence) < MIN_EVIDENCE_CHARS:
            meta["unverifiable"] += 1
            continue
        if normalize_whitespace(evidence) not in normalize_whitespace(span.text):
            meta["unverifiable"] += 1
            continue
        verified.append(issue)

    grouped = _merge_systematic(verified, meta)

    threshold = SEVERITY_RANK.get(min_severity, 1)
    kept = [
        issue
        for issue in grouped
        if SEVERITY_RANK.get(issue.get("severity", "medium"), 1) <= threshold
    ]
    meta["low_severity"] = len(grouped) - len(kept)

    kept.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 2), item.get("span_label", "")))
    return kept[:max_issues], meta


def _merge_systematic(
    issues: list[dict[str, Any]], meta: dict[str, int]
) -> list[dict[str, Any]]:
    """把"同一类系统性毛病"合并成一条，避免同一个毛病被报 N 次。"""

    groups: list[dict[str, Any]] = []
    for issue in issues:
        matched = None
        for group in groups:
            if group["category"] != issue["category"]:
                continue
            if _same_kind(group["problem"], issue["problem"]):
                matched = group
                break
        if matched is None:
            item = dict(issue)
            item["affected_spans"] = [issue.get("span_label")] if issue.get("span_label") else []
            item["affected_ids"] = [issue.get("id")] if issue.get("id") is not None else []
            groups.append(item)
            continue

        if issue.get("span_label") and issue["span_label"] not in matched["affected_spans"]:
            matched["affected_spans"].append(issue["span_label"])
        if issue.get("id") is not None:
            matched["affected_ids"].append(issue["id"])
        # 保留更严重的等级
        if SEVERITY_RANK.get(issue["severity"], 2) < SEVERITY_RANK.get(matched["severity"], 2):
            matched["severity"] = issue["severity"]
        meta["merged_systematic"] += 1

    for group in groups:
        if len(group["affected_spans"]) > 1:
            others = "、".join(group["affected_spans"][1:6])
            suffix = f"（同类问题共 {len(group['affected_spans'])} 处，另见 {others}）"
            if suffix not in group["problem"]:
                group["problem"] = group["problem"].rstrip("。") + suffix
    return groups


def _same_kind(left: str, right: str) -> bool:
    """
    两条问题算不算"同一类系统性毛病"。

    只看整体相似度会漏：例如
      "参考文献格式不统一，缺少出版信息"
      "参考文献格式不统一，混用了两种著录方式"
    整体相似度不一定够，但开头完全一样。所以再加一条"共同前缀"判据。
    """

    left, right = left.strip(), right.strip()
    if not left or not right:
        return False
    if similarity(left, right) >= SYSTEMATIC_SIMILARITY:
        return True

    common = 0
    for first, second in zip(left, right):
        if first != second:
            break
        common += 1
    return common >= 6
