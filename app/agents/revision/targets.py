"""Select actionable issues and gather their full-document context."""

from __future__ import annotations

import re
from typing import Any

from app.document.spans import (
    Span,
    build_spans,
    chunk_spans,
    resolve_span,
)
from app.utils.text import (
    looks_like_code,
    similarity,
)


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
    from app.document.review_items import decision_for

    remaining = [
        i
        for i in remaining
        if not (decision_for(i, state.get("selected_decisions")) or {}).get("manual_reason")
    ]
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
        issue_id
        for patch in (state.get("applied_patches") or []) + (state.get("rejected_patches") or [])
        for issue_id in [patch.get("issue_id"), *(patch.get("issue_ids") or [])]
    }
    if findings:
        pending = [
            issue
            for issue in findings
            if issue.get("id") not in handled_issue_ids
            and not issue.get("ignored")
            and not issue.get("deferred")
            and not issue.get("manual_required")
        ]
        if pending:
            return pending
        # Existing findings were handled, deferred or declined. A generic
        # direct-revision fallback would reopen those decisions.
        if not (int(state.get("turn", 1)) > 1 and int(state.get("iteration", 0)) == 0):
            return []

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


def _absence_reason(reason: str) -> bool:
    return bool(
        re.search(
            r"(?:本块|本段|本片段|当前片段|本轮片段).{0,160}(?:未出现|并未出现|未检索到|不含|没有出现|无可修改)",
            reason,
        )
    )


def _missing_context(reason: str) -> bool:
    return bool(
        re.search(
            r"(?:不在|未包含在|未提供|未包含).{0,30}(?:本片段|当前片段|本块|本轮|上下文)|"
            r"(?:本片段|当前片段|本块).{0,30}(?:未包含|未提供)|超出本片段范围",
            reason,
        )
    )


def _related_spans(spans: list[Span], issue: dict[str, Any]) -> list[Span]:
    """Resolve both ends of a cross-section issue against the actual full text."""
    text = " ".join(
        str(issue.get(k) or "")
        for k in ("location", "problem", "suggestion", "selected_option", "context_hint")
    )
    labels = set(issue.get("other_labels") or []) | set(issue.get("span_labels") or [])
    labels.update(re.findall(r"(?<![A-Za-z0-9_])[PHTL]\d+(?![A-Za-z0-9_])", text))
    sections = {
        s.section
        for s in spans
        if s.section
        and len(s.section.lstrip("# ").strip()) > 2
        and s.section.lstrip("# ").strip() in text
    }
    abstract = bool(re.search(r"摘\s*要|\babstract\b", text, re.I))
    return [
        s
        for s in spans
        if s.label in labels or s.section in sections or (abstract and s.zone == "abstract")
    ]


def _scope_terms(issue: dict[str, Any]) -> list[str]:
    """Scope a terminology convention by actual occurrences, never its fake anchor."""
    if issue.get("terminology_concept") and issue.get("scope_terms"):
        return list(issue["scope_terms"])
    text = " ".join(str(issue.get(k) or "") for k in ("problem", "suggestion", "selected_option"))
    if not re.search(
        r"全文统一为|术语|混用|首次出现|写法不一致|(?:名称|缩写|大小写).{0,10}统一|统一.{0,10}(?:写法|术语|名称|缩写|大小写)",
        text,
    ):
        return []
    terms = re.findall(
        r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:[ -][A-Za-z]+)*(?![A-Za-z0-9_])", text
    )
    terms = [t for t in terms if len(t) > 1 and not re.fullmatch(r"[RPHST]\d+", t)]
    terms.extend(
        t
        for t in re.findall(r"[「“\"]([^」”\"\n]{1,12})[」”\"]", text)
        if not re.search(r"统一|首次|修改|后续", t)
    )
    if re.search(r"\bbatch\b|\bepoch\b", text, re.I):
        terms.extend(["batch", "batch size", "epoch", "批次", "批大小", "批次大小", "训练轮次"])
    return list(dict.fromkeys(terms))


def _term_in(text: str, term: str) -> bool:
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", text, re.I))


def _scope_targets(spans: list[Span], targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scoped = []
    for issue in targets:
        terms = _scope_terms(issue)
        if terms:
            matches = [
                s
                for s in spans
                if not looks_like_code(s.text) and any(_term_in(s.text, t) for t in terms)
            ]
            if not matches:
                if any(t.lower() in {"batch", "epoch", "batch size"} for t in terms):
                    # This explicit family includes both English and Chinese
                    # aliases. None occur: there is no terminology work here.
                    continue
                # Unknown aliases may still exist (e.g. a Chinese name being
                # changed to an English one). Do not infer absence from the
                # requested target spelling alone.
                scoped.append(issue)
                continue
            first = {t: next((s.label for s in matches if _term_in(s.text, t)), "") for t in terms}
            issue = {
                **issue,
                "scope_terms": terms,
                "span_labels": [s.label for s in matches],
                "span_id": matches[0].id,
                "span_label": matches[0].label,
                "evidence": matches[0].text,
                "first_occurrence_labels": first,
            }
        scoped.append(issue)
    return scoped


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
        if issue.get("scope_terms"):
            for position, chunk in enumerate(chunks):
                labels = [s.label for s in chunk if s.label in issue["span_labels"]]
                if labels:
                    buckets.setdefault(position, []).append(
                        {**issue, "chunk_target_labels": labels}
                    )
            continue
        span = _locate_span(spans, issue)
        if span is None:
            # An unlocated whole-document instruction is not a first-chunk task.
            for position in range(len(chunks)):
                buckets.setdefault(position, []).append(issue)
            continue
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
        for other in _related_spans(spans, issue):
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
            chunk.extend(span for span in spans if span.id in wanted and span.id not in have)
            chunk.sort(key=lambda item: item.index)
        groups.append((chunk, buckets[index]))
    return groups


def prepare_targets(state, spans):
    """Merge explicit requirements, honor choices, and separate manual work."""
    targets = collect_targets(state)
    from app.document.requests import explicit_targets

    explicit = explicit_targets(state, spans)
    if explicit:
        from app.agents.orchestrator import requests_full_review

        if requests_full_review(state.get("user_request")):
            targets = [
                {
                    **i,
                    "suggestion": state["user_request"]
                    + "。已由系统逐项生成明确的格式补丁，本项仅处理其余内容审查修改，不要重复生成格式操作。",
                }
                if i.get("source") == "user_request"
                else i
                for i in targets
            ]
        else:
            targets = [i for i in targets if i.get("source") != "user_request"]
    for target in explicit:
        done = [
            p
            for row in state.get("revision_history") or []
            for p in row.get("applied") or []
            if str(p.get("issue_id")) == str(target["id"])
        ]
        done += [
            p
            for p in state.get("applied_patches") or []
            if str(p.get("issue_id")) == str(target["id"])
        ]
        unique = {p.get("patch_id"): p for p in done}
        needed = target.get("requested_count", len(target.get("span_labels") or []))
        if needed and len(unique) >= needed:
            continue
        target = {**target, "remaining_count": max(0, needed - len(unique))}
        if target.get("requested_count"):
            target["suggestion"] += (
                f" 本轮还需 {target['remaining_count']} 个，不能因其他审查问题而遗漏。"
            )
        targets = [i for i in targets if i.get("id") != target["id"]] + [target]
    from app.document.review_items import (
        decision_for,
        manual_item,
        manual_reason,
    )

    manual = list(state.get("manual_items") or [])
    requested_manual = manual_reason(state.get("user_request"))
    if (
        requested_manual
        and targets
        and not any(
            i.get("source") in {"user_request", "explicit_citation", "explicit_table"}
            for i in targets
        )
    ):
        manual.append(
            manual_item(
                {
                    "problem": state["user_request"],
                    "suggestion": state["user_request"],
                    "source": "user_request",
                },
                requested_manual,
            )
        )
    automatic = []
    for issue in targets:
        choice = decision_for(issue, state.get("selected_decisions"))
        if choice:
            issue = {
                **issue,
                "selected_option": choice["selected_option"],
                "suggestion": choice["selected_option"],
            }
        from app.document.operation_policy import CITATION_DISABLED, TABLE_DISABLED

        source = issue.get("source")
        reason = (
            CITATION_DISABLED
            if source == "explicit_citation"
            else TABLE_DISABLED
            if source == "explicit_table"
            else ""
            if source == "explicit_format"
            else (choice or {}).get("manual_reason")
            or manual_reason(issue.get("suggestion") or issue.get("problem"))
        )
        if reason:
            manual.append(manual_item(issue, reason))
        else:
            automatic.append(issue)
    targets = _scope_targets(spans, automatic)
    return targets, manual
