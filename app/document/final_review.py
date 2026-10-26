"""Compose multi-round edits against the immutable original before final review.

Each checkbox controls one independent original→final replacement. Never replay
dependent intermediate patches after a user has deselected their prerequisite.
"""

import re
from hashlib import sha256

from app.document.patches import changes_code
from app.document.spans import build_spans, resolve_span


def compose_changes(state):
    original = state.get("original_document") or ""
    text = original
    origins = [(i, i + 1) for i in range(len(original))]
    groups = []
    structural = []
    rounds = [
        row.get("applied") or []
        for row in state.get("revision_history") or []
        if row.get("stage") == "apply"
    ]
    if not rounds:
        rounds = [state.get("applied_patches") or []]
    for items in rounds:
        spans = build_spans(text)
        edits = []
        for item in items:
            span = resolve_span(
                spans, span_id=item.get("span_id", ""), label=item.get("span_label", "")
            )
            if span is None:
                raise ValueError("历史修改的段落无法定位，不能安全生成最终修改")
            if item.get("action", "replace_text") != "replace_text":
                # Keep original paragraph provenance even when earlier textual
                # revisions changed its content-addressed span id.
                a, b = span.start, span.end
                lo = min(pair[0] for pair in origins[a:b])
                hi = max(pair[1] for pair in origins[a:b])
                structural.append((lo, hi, item))
                continue
            a = span.start + int(item["start_in_span"])
            b = span.start + int(item["end_in_span"])
            before = item.get("before") or item.get("quote") or ""
            after = "" if item.get("delete") else str(item.get("replace") or "")
            if not (span.start <= a < b <= span.end) or text[a:b] != before:
                raise ValueError("历史修改与原文版本不一致，已停止自动写入")
            if changes_code(before, after, span.text):
                raise ValueError("历史修改触及代码保护范围，已停止自动写入")
            edits.append((a, b, after, item))
        ordered = sorted(edits, key=lambda edit: (edit[0], edit[1]))
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("同轮修改范围重叠，不能独立确认")
        for a, b, after, item in reversed(ordered):
            lo = min(pair[0] for pair in origins[a:b])
            hi = max(pair[1] for pair in origins[a:b])
            merged = [g for g in groups if g["start"] < hi and lo < g["end"]]
            sources = [item]
            for group in merged:
                lo, hi = min(lo, group["start"]), max(hi, group["end"])
                sources = group["sources"] + sources
                groups.remove(group)
            groups.append({"start": lo, "end": hi, "sources": sources})
            origins[a:b] = [(lo, hi)] * len(after)
            text = text[:a] + after + text[b:]
        # apply_patches collapses blank lines after each round. Keep the
        # character provenance synchronized with precisely that normalization.
        for match in reversed(list(re.finditer(r"\n{3,}", text))):
            start, end = match.span()
            text = text[: start + 2] + text[end:]
            del origins[start + 2 : end]
    if text != state.get("current_document", original):
        raise ValueError("最终文本与修改历史不一致，不能安全确认")
    spans = build_spans(original)
    patches = []
    for group in sorted(groups, key=lambda g: g["start"]):
        lo, hi = group["start"], group["end"]
        before = original[lo:hi]
        after = "".join(char for char, (a, b) in zip(text, origins) if a < hi and lo < b)
        if before == after:
            continue
        span = next((s for s in spans if s.start <= lo and hi <= s.end), None)
        if span is None:
            raise ValueError("最终修改跨越多个段落，无法安全独立确认")
        sources = group["sources"]
        identity = f"{lo}:{hi}:{before}:{after}"
        patches.append(
            {
                "patch_id": "final-" + sha256(identity.encode()).hexdigest()[:16],
                "source_patch_ids": list(
                    dict.fromkeys(
                        i
                        for p in sources
                        for i in [p.get("patch_id"), *(p.get("source_patch_ids") or [])]
                        if i
                    )
                ),
                "issue_ids": list(
                    dict.fromkeys(
                        i
                        for p in sources
                        for i in [p.get("issue_id"), *(p.get("issue_ids") or [])]
                        if i is not None
                    )
                ),
                "span_id": span.id,
                "span_label": span.label,
                "position": span.label,
                "start_in_span": lo - span.start,
                "end_in_span": hi - span.start,
                "original_start": lo,
                "original_end": hi,
                "quote": before,
                "before": before,
                "replace": after,
                "delete": not bool(after),
                "status": "applied",
                "rationale": "；".join(
                    dict.fromkeys(p.get("rationale") or "" for p in sources if p.get("rationale"))
                ),
            }
        )
    from app.document.structured import signature

    seen = set()
    for lo, hi, item in structural:
        span = next((s for s in spans if s.start <= lo and hi <= s.end), None)
        if span is None:
            raise ValueError("结构修改无法映射回原稿段落")
        before = item.get("before") or item.get("quote") or span.text
        if before not in span.text:
            # A structural operation must not depend on accepting another edit.
            raise ValueError("结构修改依赖了中间文本版本，不能独立确认")
        start = span.start + span.text.index(before)
        key = (span.id, before, signature(item))
        if key in seen:
            continue
        seen.add(key)
        identity = repr(key)
        patches.append(
            {
                **item,
                "patch_id": "final-" + sha256(identity.encode()).hexdigest()[:16],
                "source_patch_ids": [item["patch_id"], *(item.get("source_patch_ids") or [])],
                "issue_ids": list(
                    dict.fromkeys(
                        i
                        for i in [item.get("issue_id"), *(item.get("issue_ids") or [])]
                        if i is not None
                    )
                ),
                "span_id": span.id,
                "span_label": span.label,
                "before": before,
                "quote": before,
                "original_start": start,
                "original_end": start + len(before),
                "start_in_span": start - span.start,
                "end_in_span": start - span.start + len(before),
            }
        )
    return patches


def unfinished_items(state):
    if state.get("visual_review"):
        # Only reviewed edits can be unfinished edits. Audit candidates and
        # diagnostics are not a promise to the user that an edit will be made.
        result = [dict(item) for item in state.get("final_manual_items") or []]
        # Review uncertainty is not a writeback failure. Concrete unresolved
        # questions belong before confirmation; export appends actual failures.
        from app.document.review_items import location_map, readable

        locations = location_map(state.get("current_document") or state.get("original_document"))
        for item in result:
            item["reason"] = readable(item.get("reason"), locations)
        return deduplicate(result)
    items = []

    def add(title, reason, position="", kind="unresolved", **extra):
        items.append(
            {
                "title": str(title),
                "reason": str(reason),
                "position": str(position),
                "kind": kind,
                **extra,
            }
        )

    review = state.get("review_result") or {}
    for issue in review.get("remaining_issues") or []:
        add(
            issue.get("problem") or "问题尚未解决",
            issue.get("suggestion") or "复核仍未通过，需作者核对",
            issue.get("span_label") or issue.get("location") or "",
        )
    for violation in review.get("constraint_violations") or []:
        add("修改约束尚未通过复核", violation, kind="verification")
    for issue in review.get("per_issue") or []:
        if issue.get("status") in {"unresolved", "partially"}:
            add(f"问题 {issue.get('id')} 未完全解决", issue.get("reason") or "复核未通过")
    if review.get("status") == "fail" and not items:
        add(
            "整体复核未通过",
            review.get("summary") or "无法确认所有修改正确，请逐项核对",
            kind="verification",
        )
    applied_issues = {p.get("issue_id") for p in state.get("applied_patches") or []}
    for history in state.get("revision_history") or []:
        applied_issues.update(p.get("issue_id") for p in history.get("applied") or [])
    for issue in (state.get("audit_findings") or []) + (state.get("deferred_advice") or []):
        if issue.get("ignored") or issue.get("deferred"):
            add(
                issue.get("problem") or "未确认的问题",
                issue.get("deferred_reason") or "用户未选择该口径，保持原文",
                issue.get("span_label") or "",
            )
        elif issue.get("kind", "fix") != "advice" and issue.get("id") not in applied_issues:
            add(
                issue.get("problem") or "修改尚未落实",
                "未生成可落实的修改；请作者结合原文核对",
                issue.get("span_label") or issue.get("location") or "",
            )
    conflicts = list(state.get("conflict_patches") or [])
    for history in state.get("revision_history") or []:
        conflicts.extend(history.get("failed_edits") or [])
    applied = {p.get("patch_id") for p in state.get("applied_patches") or []}
    for patch in conflicts:
        if patch.get("patch_id") not in applied:
            add(
                patch.get("before") or patch.get("quote") or "修改未落实",
                patch.get("conflict_reason") or patch.get("reason") or "无法安全定位原文",
                patch.get("span_label") or "",
                "blocked",
            )
    for error in state.get("errors") or []:
        add("处理限制", error, kind="verification")
    meta = state.get("audit_meta") or {}
    if (
        meta.get("budget_stopped")
        or meta.get("truncated")
        or meta.get("chunks_dropped")
        or meta.get("failed_chunks")
        or (state.get("budget") or {}).get("exceeded")
    ):
        add("检查范围不完整", "达到预算或处理上限，不能声称全文已经检查完成", kind="verification")
    return deduplicate(items)


def deduplicate(items):
    seen, result = set(), []
    for item in items:
        key = (
            item.get("patch_id"),
            item.get("title"),
            item.get("reason"),
            item.get("position"),
            item.get("kind"),
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def visible_unfinished(items, reviewed_ids=None, reviewed_manual_ids=None):
    """Enforce provenance at output boundaries, including old saved summaries.

    Legacy not_selected entries were only ever emitted at the final gate.
    Other legacy rows without a review ID cannot be asserted to be reviewed.
    """
    allowed = set(reviewed_ids or [])
    manual = set(reviewed_manual_ids or [])
    return [
        item
        for item in items or []
        if (item.get("patch_id") in allowed and item.get("kind") in {"blocked", "not_selected"})
        or (item.get("manual_id") in manual and item.get("kind") == "manual")
        or (reviewed_ids is None and item.get("kind") == "not_selected")
    ]


def unfinished_section(items):
    lines = [
        "## 未完成的修改及原因",
        "",
        "列出最终确认中已告知的人工修改事项，以及确认后未写入或被取消的修改。",
        "",
    ]
    for index, item in enumerate(items, 1):
        lines.append(
            f"{index}. {item.get('position', '')} {item.get('title', '')}：{item.get('reason', '')}"
        )
        if item.get("kind") in {"manual", "blocked"}:
            lines.extend(
                [
                    f"   无法修改，需人工修改。原因：{item.get('blocked_reason') or item.get('reason', '')}",
                    f"   修改建议：{item.get('suggestion') or '请结合原文人工完成此项修改'}",
                ]
            )
    if not items:
        return ""
    return "\n".join(lines) + "\n\n"


def refresh_unfinished_section(report, items):
    """Refresh just this section in an older report, preserving the audit log."""
    return re.sub(
        r"^## 未完成的修改及原因\r?\n.*?(?=^## |\Z)",
        lambda _: unfinished_section(items),
        report,
        count=1,
        flags=re.M | re.S,
    )
