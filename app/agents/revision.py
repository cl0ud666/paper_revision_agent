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

import json
import re
from functools import partial
from typing import Any, Callable

from app.agents.prompts import REVISION_SYSTEM, render_patch_prompt
from app.config import get_settings
from app.document.patches import (
    STATUS_REJECTED,
    Patch,
    coalesce_compatible_patches,
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
    from app.document.requests import explicit_targets, format_patches

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
    from app.document.structured import document_capabilities

    structure_inventory = []
    if state.get("source_docx"):
        try:
            structure_inventory = document_capabilities(state["source_docx"])
        except (OSError, ValueError):
            pass
    from app.document.review_items import (
        decision_conflict,
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
    format_targets = [i for i in targets if i.get("source") == "explicit_format"]

    if not document.strip() or not targets:
        reason = "论文内容为空。" if not document.strip() else "没有需要修改的问题。"
        log(f"[Revision] {reason}")
        return {
            "manual_items": manual,
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

    groups = _group_targets(
        spans, [i for i in targets if i not in format_targets], settings.context.revision_max_tokens
    )
    groups = [(chunk, issues) for chunk, issues in groups if issues]
    log(
        f"[Revision] 本轮需要处理 {len(targets)} 个问题，"
        f"分 {len(groups)} 批提出补丁（一次性汇总给用户）。"
    )

    guard = get_guard("revision", on_event=log)
    counter = int(state.get("patch_counter", 0))
    issues_by_id = {issue.get("id"): issue for issue in targets}
    patches: list[Patch] = []
    for target in format_targets:
        for patch in format_patches(target, spans):
            counter += 1
            patch.patch_id = next_patch_id(counter)
            validate_patch(patch, spans)
            patches.append(patch)
    notes: list[str] = []
    failed_batches = 0
    budget_stopped = False
    budget_snapshot: dict[str, Any] | None = None
    context_retried: set[str] = set()
    applicability: dict[str, list[bool]] = {}

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
                "word_structure": [
                    item
                    for item in structure_inventory
                    if item["span_label"] in {s.label for s in chunk}
                ],
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
            bad, valid = [], []
            for candidate in data.get("patches") or []:
                if not isinstance(candidate, dict):
                    continue
                probe = patch_from_model(candidate, spans=spans, patch_id="validate-only")
                validate_patch(probe, spans)
                if (candidate.get("quote") or candidate.get("before")) and probe.conflict_code in {
                    "empty_replace",
                    "empty",
                    "not_found",
                    "no_span",
                }:
                    bad.append({"patch": candidate, "error": probe.conflict_reason})
                else:
                    valid.append(candidate)
            if bad:
                repair_prompt = (
                    prompt
                    + "\n\n# 本批校验反馈（只重做下面的无效补丁，不能重发已成功条目）\n"
                    + json.dumps(bad, ensure_ascii=False)
                    + "\n重新从上方原文逐字选择 quote，给出具体修改后内容；不要凭空设为删除。确实无需修改时返回 not_applicable。"
                )
                try:
                    check(state, agent="revision", stage=f"repair_{batch_index}")
                    repaired = guard.run(
                        partial(
                            call_llm,
                            repair_prompt,
                            "revision",
                            system=REVISION_SYSTEM,
                            json_mode=True,
                            run_id=run_id,
                            action=f"repair_{batch_index}",
                        ),
                        action=f"repair_{batch_index}",
                        recorder=recorder,
                        run_id=run_id,
                        iteration=iteration,
                    )
                    repaired_data = parse_json_object(
                        repaired.text, required_keys=("patches",), context="Revision repair"
                    )
                    data["patches"] = valid + list(repaired_data.get("patches") or [])
                    data["unfulfilled"] = list(data.get("unfulfilled") or []) + list(
                        repaired_data.get("unfulfilled") or []
                    )
                except Exception as exc:
                    log(f"[Revision] 本批补丁校验重试未完成：{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响其它批次
            failed_batches += 1
            log(f"[Revision] 第 {batch_index} 批提出补丁失败：{type(exc).__name__}: {exc}")
            continue

        note = str(data.get("notes") or "").strip()
        if note:
            notes.append(note)
        for issue in chunk_issues:
            applicability.setdefault(str(issue.get("id")), []).append(False)
        for blocked in data.get("unfulfilled") or []:
            if not isinstance(blocked, dict):
                continue
            issue = next(
                (i for i in chunk_issues if str(i.get("id")) == str(blocked.get("issue_id"))), None
            )
            if issue:
                reason = str(blocked.get("reason") or "")
                if blocked.get("status") == "missing_context" or _missing_context(reason):
                    key = str(issue.get("id"))
                    if key not in context_retried:
                        context_retried.add(key)
                        enriched = {
                            **issue,
                            "context_hint": reason,
                            "other_labels": [
                                *(issue.get("other_labels") or []),
                                *(blocked.get("target_labels") or []),
                            ],
                        }
                        related = _related_spans(spans, enriched)
                        anchor = _locate_span(spans, issue)
                        if anchor and anchor not in related:
                            related.append(anchor)
                        if related:
                            groups.append((sorted(related, key=lambda s: s.index), [issue]))
                            log("[Revision] 已从全文补齐关联段落，重新生成该问题的修改方案。")
                            continue
                    manual.append(
                        manual_item(
                            issue,
                            "已检索全文关联段落并重试，但未生成可安全执行的修改方案；原文保持不变",
                            issue.get("suggestion"),
                        )
                    )
                    continue
                # Absence in a chunk is not an inability to edit. In particular,
                # a global terminology rule must never become a cover-page TODO.
                if blocked.get("status") == "not_applicable" or _absence_reason(reason):
                    applicability[str(issue.get("id"))][-1] = True
                    continue
                if issue.get("scope_terms"):
                    evidence = str(blocked.get("quote") or "")
                    anchor = resolve_span(chunk, label=str(blocked.get("span_label") or ""))
                    if not evidence or anchor is None or evidence not in anchor.text:
                        continue
                    issue = {
                        **issue,
                        "span_id": anchor.id,
                        "span_label": anchor.label,
                        "evidence": evidence,
                    }
                manual.append(
                    manual_item(
                        issue,
                        str(blocked.get("reason") or "未生成可安全落实的修改"),
                        blocked.get("suggestion") or issue.get("suggestion"),
                    )
                )

        for item in data.get("patches") or []:
            if not isinstance(item, dict):
                continue
            if not any(
                item.get(k)
                for k in ("quote", "before", "original", "replace", "after", "params", "delete")
            ):
                notes.append("模型返回空补丁，已忽略并按对应要求核查完成情况。")
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
            if patch.issue_id is None and len(chunk_issues) == 1:
                patch.issue_id = chunk_issues[0].get("id")
            validate_patch(patch, spans)
            patches.append(patch)

    # 用户在闸门里定了「全文统一为 X」的词级口径 → **由程序**把全文替换一遍。
    # 不走模型：位置要穷尽（这篇 40+ 处）、替换正确性可判定、且代码不许碰。
    for patch in _unification_patches(spans, state.get("user_rules")):
        counter += 1
        patch.patch_id = next_patch_id(counter)
        validate_patch(patch, spans)
        if patch.issue_id is None:
            target = next(
                (
                    i
                    for i in targets
                    if i.get("source") == "naming_variants"
                    and patch.span_label in [i.get("span_label"), *(i.get("span_labels") or [])]
                ),
                None,
            )
            if target:
                patch.issue_id = target.get("id")
        patches.append(patch)

    already_satisfied = set()
    for issue in targets:
        related_before_filter = [p for p in patches if str(p.issue_id) == str(issue.get("id"))]
        if related_before_filter and all(
            p.conflict_code in {"no_change", "code_protected"} for p in related_before_filter
        ):
            already_satisfied.add(str(issue.get("id")))
    patches = _drop_code_patches(patches, spans, notes, log)
    patches = _drop_noop_patches(patches, notes, log)
    patches = _mark_previously_rejected(patches, state)
    for patch in patches:
        reason = decision_conflict(patch, state.get("selected_decisions"), spans)
        if reason:
            patch.status, patch.conflict_reason = "conflict", reason
    patches = coalesce_compatible_patches(patches, spans)
    for issue in targets:
        if str(issue.get("id")) in already_satisfied:
            continue
        related = [
            p
            for p in patches
            if str(issue.get("id")) in {str(i) for i in [p.issue_id, *p.issue_ids]}
        ]
        if applicability.get(str(issue.get("id"))) and all(applicability[str(issue.get("id"))]):
            continue
        proposed = [p for p in related if p.status == "proposed"]
        if issue.get("requested_count") and len(proposed) < issue.get("remaining_count", 1):
            missing = issue["remaining_count"] - len(proposed)
            manual.append(
                manual_item(
                    issue,
                    f"本轮还有 {missing} 个引用未生成或未通过来源核验，不能声称已完成全部引用要求",
                    f"补充 {missing} 个有真实来源且与正文论据相关的引用",
                )
            )
        elif not proposed and not any(m.get("id") == issue.get("id") for m in manual):
            reason = next((p.conflict_reason for p in related if p.conflict_reason), "")
            if issue.get("source") == "explicit_format" and not issue.get("span_labels"):
                reason = "未在正文中可靠定位到所要求的标题层级，请核对标题编号或标题样式；未将正文段落当成标题修改"
            manual.append(
                manual_item(
                    issue, reason or "本轮未生成可安全执行的修改，无法保证修改准确；需要人工完成"
                )
            )
    stats = summarize_patches(patches)
    summary = _build_summary(targets, patches, notes, failed_batches, budget_stopped)

    log(
        f"[Revision] 提出 {len(patches)} 条补丁"
        f"（可接受 {stats.get('proposed', 0)} 条，无法定位 {stats.get('conflict', 0)} 条）。"
    )

    return {
        "manual_items": manual,
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
                    patch.to_json() for patch in patches if patch.status == "conflict"
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
                    quote = _unique_quote(span.text, match.start(), match.end(), token)
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
    """Only identical edits at the same position are duplicates."""

    seen: dict[tuple, Patch] = {}
    result: list[Patch] = []
    for patch in patches:
        key = (
            patch.span_id,
            patch.start_in_span,
            patch.end_in_span,
            patch.before,
            patch.quote,
            patch.replace,
            patch.delete,
            patch.status,
            patch.issue_id,
        )
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
