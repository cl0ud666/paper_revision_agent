"""
LangGraph Node层。

每个Node只做三件事：

1. 调用对应的Agent；
2. 处理异常（单个Agent失败不能拖垮整条Workflow，需要降级）；
3. 写回State（包含Trace事件）。

Human Approval不是一个Agent，而是这里的一个人工确认节点，
通过LangGraph的interrupt机制暂停Workflow，等用户输入后再恢复。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from langgraph.types import interrupt

from app.agents import (
    audit as audit_agent,
    orchestrator as orchestrator_agent,
    report as report_agent,
    review as review_agent,
    revision as revision_agent,
)
from app.config import get_settings
from app.document.patches import (
    STATUS_ACCEPTED,
    STATUS_CONFLICT,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    Patch,
    apply_patches,
)
from app.document.spans import build_spans, index_summary, resolve_span
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.tracing import get_recorder, utc_now

# Re-export payload helpers for clients using the original node module.
from app.workflow.payloads import (
    _excluded_ids as _excluded_ids,
    _patch_line as _patch_line,
    _public_issue as _public_issue,
    _public_patch as _public_patch,
    normalize_human_payload as normalize_human_payload,
    normalize_issue_payload as normalize_issue_payload,
    normalize_patch_payload as normalize_patch_payload,
)


def _logger(logger: Callable[[str], None] | None = None) -> Callable[[str], None]:
    return logger or print


def _event(state: Any, action: str, *, ok: bool = True, **extra: Any) -> dict[str, Any]:
    """构造一条写进State的Trace事件（流程类，不计入LLM统计）。"""

    return {
        "agent": "workflow",
        "action": action,
        "iteration": int(state.get("iteration", 0)),
        "ok": ok,
        "extra": extra,
    }


def _recorder(state: Any):
    return get_recorder(state.get("run_id") or "adhoc")


# ---------------------------------------------------------------- 节点实现


def orchestrator_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """初始规划 / 自适应重规划。"""

    log = _logger(logger)
    # 任务开始时间（预算计时用）：只在第一次进入时写
    run_started_at = state.get("run_started_at") or utc_now()

    try:
        check(state, agent="orchestrator", stage="plan")
    except BudgetExceeded as exc:
        log(f"[Orchestrator] 预算不足，直接收敛出报告：{exc.reason}")
        update = orchestrator_agent.fallback_plan(state)
        update["next_action"] = "report"
        update["plan_reason"] = f"预算不足，直接输出报告：{exc.reason}"
        return {
            **update,
            "run_started_at": run_started_at,
            "budget": exceeded_snapshot(state, exc),
            "errors": [f"预算降级：{exc.reason}"],
            "trace": [_event(state, "orchestrator", next_action="report", budget=True)],
        }

    try:
        update = orchestrator_agent.plan(state, recorder=_recorder(state), logger=log)
    except Exception as exc:  # noqa: BLE001 - 规划失败必须降级
        log(f"[Orchestrator] 规划异常：{type(exc).__name__}: {exc}")
        update = orchestrator_agent.fallback_plan(state)
        update["errors"] = [f"Orchestrator规划异常：{type(exc).__name__}: {exc}"]

    return {
        **update,
        "run_started_at": run_started_at,
        "budget": usage(state),
        "trace": [_event(state, "orchestrator", next_action=update.get("next_action"))],
    }


def audit_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """系统性审查论文。"""

    log = _logger(logger)
    try:
        update = audit_agent.run(state, recorder=_recorder(state), logger=log)
    except Exception as exc:  # noqa: BLE001
        log(f"[Audit] 审查异常：{type(exc).__name__}: {exc}")
        update = {
            "audit_findings": [],
            "audit_failed": True,
            "audit_meta": {"error": str(exc)},
            "errors": [f"Audit审查异常：{type(exc).__name__}: {exc}"],
        }

    if state.get("visual_review"):
        deferred = list(state.get("deferred_advice") or [])
        for issue in update.get("audit_findings") or []:
            if (
                state.get("decisions")
                and issue.get("kind") == "decision"
                and not issue.get("ignored")
            ):
                issue.update(
                    kind="advice",
                    deferred=True,
                    deferred_reason="口径确认已结束；后续发现的新歧义不再询问，也不擅自改动",
                )
            if issue.get("kind") == "advice":
                span = resolve_span(
                    build_spans(state.get("current_document") or ""),
                    span_id=issue.get("span_id", ""),
                    label=issue.get("span_label", ""),
                )
                issue["audit_anchor_text"] = span.text if span else issue.get("evidence", "")
                deferred.append(dict(issue))
        update["deferred_advice"] = deferred
    # 保留兼容元数据；实际只有decision findings会进入人工确认。
    issue_gate = get_settings().workflow.issue_gate
    update["issue_gate"] = issue_gate
    if issue_gate:
        update["approved_issue_ids"] = []
        update["human_decision"] = ""
    else:
        # 闸门关闭：问题直接进入补丁阶段，用户只在补丁清单上做一次决定
        update["approved_issue_ids"] = [
            issue.get("id") for issue in (update.get("audit_findings") or [])
        ]

    return {
        **update,
        "trace": [
            _event(
                state,
                "audit",
                ok=not update.get("audit_failed", False),
                issues=len(update.get("audit_findings") or []),
            )
        ],
    }


def revision_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """一次提出本轮全部修改补丁（不直接改文档）。"""

    log = _logger(logger)
    try:
        update = revision_agent.run(state, recorder=_recorder(state), logger=log)
    except Exception as exc:  # noqa: BLE001 - 提补丁失败时保留原文，交给Review判断
        log(f"[Revision] 提出补丁异常：{type(exc).__name__}: {exc}")
        update = {
            "proposed_patches": [],
            "patch_stats": {},
            "revision_summary": f"本轮提出补丁失败：{exc}",
            "errors": [f"Revision提出补丁异常：{type(exc).__name__}: {exc}"],
        }

    return {
        **update,
        "trace": [
            _event(
                state,
                "revision",
                ok=bool(update.get("proposed_patches")),
                patches=len(update.get("proposed_patches") or []),
            )
        ],
    }


def issue_review_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """
    Audit之后的问题闸门：逐条列出问题，问用户"有没有要忽略的"。

    用户的决定会**持久存进user_rules**（kind=ignore，带指纹），
    因此下一轮重新审查时不会再问同一批问题，除非用户自己改主意。

    恢复时传入的payload支持：

        {"ignore_ids": [2, 4]}            # 这几条不改
        {"ignore_all": true}              # 全部不改
        {"approved_issue_ids": [1, 3]}    # 兼容旧写法：只改这些
        {"rules": ["全文统一把「土豆」写成「马铃薯」"]}   # 顺手加要求
        {"revoke": ["R1"]} / {"update": {"R1": "改成……"}}
    """

    log = _logger(logger)
    findings = [dict(i) for i in state.get("audit_findings") or []]
    if state.get("visual_review") and state.get("decisions"):
        return {"trace": [_event(state, "issue_review", skipped=True)]}
    rules = list(state.get("user_rules") or [])
    from app.document.review_items import manual_item, public_item

    # An empty choice list is not a decision. Keep it for final manual review.
    for issue in findings:
        if issue.get("kind") == "decision":
            issue["options"] = list(
                dict.fromkeys(str(o).strip() for o in issue.get("options", []) if str(o).strip())
            )
            if len(issue["options"]) < 2:
                issue.update(
                    kind="advice",
                    manual_required=True,
                    blocked_reason="未形成可供选择的明确方案，系统无法替作者决定修改口径",
                )
    # 只把**前后矛盾、必须由作者拍板**的问题拿出来问。
    # 写错的、可以更好的都不打扰用户——那些要么自动改，要么只留批注。
    pending = [
        issue
        for issue in findings
        if (issue.get("kind") or "fix") == "decision" and not issue.get("ignored")
    ]
    remembered = [
        rule
        for rule in rules
        if rule.get("kind") == "ignore" and rule.get("status", "active") == "active"
    ]

    if not pending:
        log("[Human] 没有需要你拍板的矛盾，跳过这一步。")
        return {
            "audit_findings": findings,
            "manual_items": list(state.get("manual_items") or [])
            + [
                manual_item(i, i["blocked_reason"])
                for i in findings
                if i.get("manual_required") and not i.get("ignored")
            ],
            "trace": [_event(state, "issue_review", skipped=True, pending=0)],
        }

    request = {
        "type": "issue_review",
        "run_id": state.get("run_id", "adhoc"),
        "turn": int(state.get("turn", 1)),
        "iteration": int(state.get("iteration", 0)),
        "issues": [
            {
                **_public_issue(issue),
                "display": public_item(issue, state.get("current_document", "")),
            }
            for issue in pending
        ],
        "remembered": [{"id": rule.get("id"), "text": rule.get("text")} for rule in remembered],
        "rules": [
            {"id": rule.get("id"), "text": rule.get("text")}
            for rule in rules
            if rule.get("kind") == "rule" and rule.get("status", "active") == "active"
        ],
        "instruction": (
            "以下位置前后矛盾——**以哪一处为准由你决定**"
            "（实验事实在你手上，模型看不到）。"
            "把你的判断写下来，例如「温度系数以0.07为准」"
            "「96.67% 是6-way 5-shot的分类准确率」；"
            "直接回车 = 这些先不改。"
        ),
        "defaults": {"ignore_ids": [], "rules": []},
    }

    payload = interrupt(request)
    update = normalize_issue_payload(payload, findings, rules=rules, turn=int(state.get("turn", 1)))
    update["selected_decisions"] = list(state.get("selected_decisions") or []) + update.get(
        "selected_decisions", []
    )
    update["manual_items"] = list(state.get("manual_items") or []) + update.get("manual_items", [])
    ignored_ids = [
        issue.get("id") for issue in update.get("audit_findings") or [] if issue.get("ignored")
    ]
    log(
        f"[Human] 已忽略 {len(ignored_ids)} 个问题；"
        f"当前共有 {len(update.get('user_rules') or [])} 条要求/决定。"
    )

    return {
        **update,
        # 标记"已经问过"，避免同一批矛盾被反复拿出来问。
        # 用户的判断已经作为user_rules存下来了，会自动进入Revision的上下文。
        "decisions": True,
        "trace": [
            _event(
                state,
                "issue_review",
                ignored=len(ignored_ids),
                rules=len(update.get("user_rules") or []),
            )
        ],
    }


def patch_review_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """
    补丁审阅闸门：把本轮**全部**改动一次性交给用户逐条接受 / 拒绝。

    这是默认流程里唯一必需的人工节点（问题闸门默认关闭），
    因此用户只会被问一次：看看这些改动，勾选你要的。

    恢复时传入的payload形如：

        {"accept_all": True}                      # 全部接受（默认）
        {"accepted_ids": ["patch-001", "patch-003"]}   # 只接受这几条
        {"rejected_ids": ["patch-002"]}                # 只拒绝这一条
        {"decisions": [{"patch_id": "patch-001", "decision": "reject"}]}
    """

    log = _logger(logger)
    patches = state.get("proposed_patches") or []
    decidable = [patch for patch in patches if patch.get("status") == STATUS_PROPOSED]

    # 默认不打断用户：Revision已经在清单里写清了「原文 / 准备修改为 / 原因」，
    # 需要人拍板的"前后矛盾"在Audit之后也问过一次了，这里再让用户逐条勾一遍是重复劳动。
    if state.get("visual_review") or not get_settings().workflow.patch_gate:
        for patch in patches:
            log(_patch_line(patch))
        update = normalize_patch_payload({"accept_all": True}, patches)
        stats = update.get("patch_stats") or {}
        log(
            f"[Revision] 本轮 {stats.get('accepted', 0)} 条改动直接应用"
            "（补丁闸门已关闭；如需逐条确认请设PRA_PATCH_GATE=1）。"
        )
        return {
            **update,
            "trace": [
                _event(
                    state,
                    "patch_review",
                    auto=True,
                    accepted=stats.get("accepted", 0),
                )
            ],
        }

    request = {
        "type": "patch_review",
        "run_id": state.get("run_id", "adhoc"),
        "turn": int(state.get("turn", 1)),
        "iteration": int(state.get("iteration", 0)),
        "summary": state.get("revision_summary", ""),
        "patch_count": len(patches),
        "patches": [_public_patch(patch) for patch in patches],
        "decidable_ids": [patch.get("patch_id") for patch in decidable],
        "instruction": (
            "请勾选要接受的改动（默认全选）。被拒绝的改动不会写进论文，也不需要你写理由。"
        ),
        "defaults": {"accept_all": True},
    }

    payload = interrupt(request)
    update = normalize_patch_payload(payload, patches)
    stats = update.get("patch_stats") or {}
    log(
        f"[Human] 补丁决定：接受 {stats.get('accepted', 0)} 条，"
        f"拒绝 {stats.get('rejected', 0)} 条。"
    )

    return {
        **update,
        "trace": [
            _event(
                state,
                "patch_review",
                accepted=stats.get("accepted", 0),
                rejected=stats.get("rejected", 0),
            )
        ],
    }


def apply_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """把用户接受的补丁一次性应用到文档上，并重新索引段落。"""

    log = _logger(logger)
    document = state.get("current_document") or ""
    version = int(state.get("document_version", 1))
    raw_patches = state.get("proposed_patches") or []
    patches = [Patch.from_json(item) for item in raw_patches]
    from app.document.review_items import decision_conflict

    for patch in patches:
        conflict = decision_conflict(
            patch, state.get("selected_decisions"), build_spans(document, version=version)
        )
        if conflict and patch.status == STATUS_ACCEPTED:
            patch.status, patch.conflict_reason = STATUS_CONFLICT, conflict
    accepted = [patch for patch in patches if patch.status == STATUS_ACCEPTED]
    rejected = [patch for patch in patches if patch.status == STATUS_REJECTED]
    conflicts = [patch for patch in patches if patch.status == STATUS_CONFLICT]

    if not document.strip() or not accepted:
        summary = (
            f"本轮没有需要应用的改动（拒绝 {len(rejected)} 条，无法定位 {len(conflicts)} 条）。"
        )
        log(f"[Apply] {summary}")
        return {
            "revision_summary": summary,
            "conflict_patches": [patch.to_json() for patch in conflicts],
            "budget": usage(state),
            "revision_history": [
                {
                    "iteration": int(state.get("iteration", 0)),
                    "turn": int(state.get("turn", 1)),
                    "stage": "apply",
                    "edits": 0,
                    "failed": len(conflicts),
                    "summary": summary,
                    "timestamp": utc_now(),
                    "applied": [],
                    "failed_edits": [patch.to_json() for patch in conflicts],
                }
            ],
        }

    spans = build_spans(document, version=version)
    outcome = apply_patches(document, spans, patches, version_after=version + 1)
    applied_json = [patch.to_json() for patch in outcome.applied]
    skipped_json = [patch.to_json() for patch in outcome.skipped]
    all_conflicts = [patch.to_json() for patch in conflicts] + skipped_json

    summary = (
        f"应用 {len(applied_json)} 条改动；"
        f"用户拒绝 {len(rejected)} 条，无法定位 {len(all_conflicts)} 条。"
    )
    log(f"[Apply] {summary}")

    return {
        "current_document": outcome.document,
        "document_version": version + 1,
        "spans": index_summary(outcome.spans_after),
        "applied_patches": applied_json,
        "conflict_patches": all_conflicts,
        # 兼容旧字段（报告 / 服务层 / 评测仍在读它们）
        "revision_edits": applied_json,
        "revision_failed_edits": all_conflicts,
        "revision_summary": summary,
        "budget": usage(state),
        "revision_history": [
            {
                "iteration": int(state.get("iteration", 0)),
                "turn": int(state.get("turn", 1)),
                "stage": "apply",
                "edits": len(applied_json),
                "failed": len(all_conflicts),
                "summary": summary,
                "timestamp": utc_now(),
                "applied": applied_json,
                "failed_edits": all_conflicts,
            }
        ],
    }


def review_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """独立复核。"""

    log = _logger(logger)
    try:
        update = review_agent.run(state, recorder=_recorder(state), logger=log)
    except Exception as exc:  # noqa: BLE001 - 复核失败按未通过处理
        log(f"[Review] 复核异常：{type(exc).__name__}: {exc}")
        update = {
            "review_result": {
                "status": "fail",
                "summary": f"Review复核异常：{exc}",
                "remaining_issues": [],
                "constraint_violations": [],
                "dimension_scores": {},
                "confidence": 0.0,
                "error": str(exc),
            },
            "errors": [f"Review复核异常：{type(exc).__name__}: {exc}"],
        }

    status = (update.get("review_result") or {}).get("status", "fail")
    return {
        **update,
        "trace": [_event(state, "review", ok=status == "pass", status=status)],
    }


def prepare_report_node(
    state: Any, *, logger: Callable[[str], None] | None = None
) -> dict[str, Any]:
    if not state.get("visual_review") or state.get("final_review_ready"):
        return {}
    from app.document.preview import prepare_visual_review

    prepared = prepare_visual_review(state, get_settings().paths.run_dir(state["run_id"]))
    return {**prepared, "final_review_ready": True}


def report_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """
    汇总报告并落盘。

    这里是**唯一一次**把改动交给用户确认的地方：前面每一轮都在内存里改文本
    （不然下一轮Audit看到的还是旧稿），但**Word文件只在最后写一次**。
    所以确认也只需要一次——把所有轮次要写的改动一起列出来，
    用户剔除掉的那几条不写进Word。
    """

    log = _logger(logger)
    run_id = state.get("run_id") or "adhoc"
    trace_summary = get_recorder(run_id).summary()

    state = _confirm_before_writing(state, log)

    try:
        update = report_agent.write_outputs(state, trace_summary=trace_summary, logger=log)
    except Exception as exc:  # noqa: BLE001
        log(f"[Report] 生成报告失败：{type(exc).__name__}: {exc}")
        update = {"final_report": "", "errors": [f"生成报告失败：{exc}"]}

    return {
        **update,
        "excluded_patch_ids": list(state.get("excluded_patch_ids") or []),
        **(
            {
                "final_review_confirmed": True,
                "unfinished_items": update.get(
                    "unfinished_items", state.get("unfinished_items", [])
                ),
                "current_document": update.get(
                    "current_document", state.get("current_document", "")
                ),
            }
            if state.get("visual_review")
            else {}
        ),
        "trace": [_event(state, "report", finished_at=utc_now())],
    }


def _confirm_before_writing(state: Any, log: Callable[[str], None]) -> dict[str, Any]:
    """
    写入Word之前的最后一次确认。

    没有改动就直接过；有改动就把**跨轮次的全部改动**列成
    「问题 / 改为什么 / 原因」一次性给用户看，让他剔除不要的。
    """

    if state.get("visual_review"):
        if not state.get("final_review_ready"):
            raise ValueError("最终审阅清单未冻结，禁止直接导出")
        if state.get("final_review_confirmed"):
            return state
        patches = state.get("final_patches") or []
        request = {
            "type": "final_confirm",
            "visual": True,
            "run_id": state["run_id"],
            "patches": patches,
            "patch_count": len(patches),
            "preview": state.get("preview_manifest") or {},
            "manual_items": state.get("final_manual_items") or [],
            "defaults": {"excluded_ids": state.get("excluded_patch_ids") or []},
            "instruction": "勾选表示采用修改，取消勾选保留原文；确认后不再新增修改。",
        }
        payload = interrupt(request)
        excluded = _excluded_ids(payload, patches)
        from app.document.final_review import unfinished_items

        unfinished = unfinished_items({**state, "excluded_patch_ids": excluded})
        for patch in patches:
            if patch["patch_id"] in excluded:
                unfinished.append(
                    {
                        "patch_id": patch["patch_id"],
                        "title": patch["before"],
                        "reason": "用户在最终确认中取消勾选，保持原文",
                        "position": patch.get("position", ""),
                        "kind": "not_selected",
                    }
                )
        document = state["original_document"]
        for patch in sorted(patches, key=lambda p: p["original_start"], reverse=True):
            if (
                patch["patch_id"] not in excluded
                and patch.get("action", "replace_text") == "replace_text"
            ):
                document = (
                    document[: patch["original_start"]]
                    + patch["replace"]
                    + document[patch["original_end"] :]
                )
        return {
            **state,
            "excluded_patch_ids": excluded,
            "unfinished_items": unfinished,
            "current_document": document,
            "final_review_confirmed": True,
        }
    patches = list(state.get("applied_patches") or [])
    if not patches or not get_settings().workflow.final_confirm:
        return state

    request = {
        "type": "final_confirm",
        "run_id": state.get("run_id", "adhoc"),
        "turn": int(state.get("turn", 1)),
        "patch_count": len(patches),
        # 把"这条改动是冲着哪个问题去的"一起带上：
        # 用户最后看到的是「问题 / 改为什么 / 原因」三行，缺了problem就只剩分类。
        "patches": _merge_unification_items([_confirm_item(patch, state) for patch in patches]),
        "instruction": (
            "以上是本次全部改动，**确认后写入Word副本**（原文件不动）。"
            "要剔除哪几条就输入编号（例如2,5）；直接回车 = 全部写入；none = 都不写入。"
        ),
        "defaults": {"excluded_ids": list(state.get("excluded_patch_ids") or [])},
    }

    payload = interrupt(request)
    excluded = _excluded_ids(payload, patches)
    if excluded:
        log(f"[Human] 已剔除 {len(excluded)} 条改动，不会写入Word：{'、'.join(excluded)}")
    else:
        log(f"[Human] 确认写入 {len(patches)} 条改动。")
    return {**state, "excluded_patch_ids": excluded}


def _confirm_item(patch: dict[str, Any], state: Any) -> dict[str, Any]:
    """最后确认时的单条改动：补丁字段 + 对应问题的描述。"""

    item = _public_patch(patch)
    issue_id = patch.get("issue_id")
    for issue in state.get("audit_findings") or []:
        if issue.get("id") == issue_id:
            item["problem"] = issue.get("problem") or ""
            item["kind"] = issue.get("kind") or "fix"
            break
    return item


_UNIFY_IN_NOTE = re.compile(r"全文统一为「([^」]+)」")


def _merge_unification_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    把"同一个统一口径"下的几十条补丁**合并成一条展示**。

    一次"全文统一MoCo→MOCO"会产生47条补丁（每处一条，Word里要逐处留痕）。
    让用户在确认清单里读47行没有意义——合成一条、带上处数和段落列表，
    **Word里仍然是47处独立的修订痕迹**，可以逐条接受/拒绝。
    """

    merged: list[dict[str, Any]] = []
    buckets: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("strategy") != "unification":
            merged.append(item)
            continue
        match = _UNIFY_IN_NOTE.search(str(item.get("rationale") or ""))
        target = match.group(1) if match else "统一写法"
        bucket = buckets.get(target)
        if bucket is None:
            bucket = {
                **item,
                "position": "全文",
                "problem": f"同一个名称在全文有几种写法，统一为「{target}」",
                "before": "",
                "replace": f"统一为「{target}」",
                "span_labels": [],
                "count": 0,
                "patch_ids": [],
            }
            buckets[target] = bucket
            merged.append(bucket)
        bucket["count"] = int(bucket["count"]) + 1
        bucket["patch_ids"].append(item["patch_id"])
        label = str(item.get("span_label") or "")
        if label and label not in bucket["span_labels"]:
            bucket["span_labels"].append(label)
    return merged
