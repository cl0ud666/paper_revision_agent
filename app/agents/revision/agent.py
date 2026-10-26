"""Propose reviewable patches, preserving user choices and explicit constraints."""

from __future__ import annotations

import json
from functools import partial
from typing import Any, Callable

from app.agents.prompts import REVISION_SYSTEM, render_patch_prompt
from app.agents.revision.patches import (
    _build_summary,
    _drop_code_patches,
    _drop_noop_patches,
    _mark_previously_rejected,
    _unification_patches,
)
from app.agents.revision.targets import (
    _absence_reason,
    _group_targets,
    _locate_span,
    _missing_context,
    _related_spans,
    current_spans,
    prepare_targets,
)
from app.config import get_settings
from app.document.patches import (
    Patch,
    coalesce_compatible_patches,
    next_patch_id,
    patch_from_model,
    summarize_patches,
    validate_patch,
)
from app.document.requests import format_patches
from app.document.review_items import decision_conflict, manual_item
from app.document.spans import (
    index_summary,
    render_annotated,
    resolve_span,
)
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.tracing import utc_now


def _generate_batch(
    state,
    *,
    chunk,
    chunk_issues,
    spans,
    structure_inventory,
    patches,
    batch_index,
    batch_total,
    guard,
    recorder,
    log,
):
    """Build one bounded request and repair invalid candidates once."""
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
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
            "chunk_total": batch_total,
            "chunk_text": chunk_text,
            "chunk_labels": [span.label for span in chunk],
            "issues": chunk_issues,
            # 前面批次已经提出的改动：后面的批次据此对齐术语/数字口径
            "round_patches": [patch.to_json() for patch in patches],
        },
    )
    prompt = render_patch_prompt(format_context(fields))

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

    return data


def run(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """提出本轮全部补丁，返回写回 State 的字段。"""

    log = logger or print
    settings = get_settings()
    iteration = int(state.get("iteration", 0))
    turn = int(state.get("turn", 1))
    document = state.get("current_document") or ""
    spans = current_spans(state)
    targets, manual = prepare_targets(state, spans)
    from app.document.structured import document_capabilities

    structure_inventory = []
    if state.get("source_docx"):
        try:
            structure_inventory = document_capabilities(state["source_docx"])
        except (OSError, ValueError):
            pass
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

        try:
            data = _generate_batch(
                state,
                chunk=chunk,
                chunk_issues=chunk_issues,
                spans=spans,
                structure_inventory=structure_inventory,
                patches=patches,
                batch_index=batch_index,
                batch_total=len(groups),
                guard=guard,
                recorder=recorder,
                log=log,
            )
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
