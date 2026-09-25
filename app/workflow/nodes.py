"""
LangGraph Node 层。

每个 Node 只做三件事：

1. 调用对应的 Agent；
2. 处理异常（单个 Agent 失败不能拖垮整条 Workflow，需要降级）；
3. 写回 State（包含 Trace 事件）。

Human Approval 不是一个 Agent，而是这里的一个人工确认节点，
通过 LangGraph 的 interrupt 机制暂停 Workflow，等用户输入后再恢复。
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.types import interrupt

from app.agents import audit as audit_agent
from app.agents import orchestrator as orchestrator_agent
from app.agents import report as report_agent
from app.agents import review as review_agent
from app.agents import revision as revision_agent
from app.config import get_settings
from app.document.patches import (
    STATUS_ACCEPTED,
    STATUS_CONFLICT,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    Patch,
    apply_patches,
)
from app.document.spans import build_spans, index_summary
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.rules import apply_payload as apply_rules_payload
from app.utils.rules import rule_texts
from app.utils.tracing import get_recorder, utc_now


def _logger(logger: Callable[[str], None] | None = None) -> Callable[[str], None]:
    return logger or print


def _event(state: Any, action: str, *, ok: bool = True, **extra: Any) -> dict[str, Any]:
    """构造一条写进 State 的 Trace 事件（流程类，不计入 LLM 统计）。"""

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


def orchestrator_node(
    state: Any, *, logger: Callable[[str], None] | None = None
) -> dict[str, Any]:
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
        update["errors"] = [f"Orchestrator 规划异常：{type(exc).__name__}: {exc}"]

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
            "errors": [f"Audit 审查异常：{type(exc).__name__}: {exc}"],
        }

    # 新一轮审查产生的问题默认都未批准，必须重新经过人工确认
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


def human_approval_node(
    state: Any, *, logger: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """
    人机协作节点：暂停 Workflow，把问题清单交给用户确认。

    恢复时传入的 payload 形如：

        {"decision": "approve",
         "approved_issue_ids": [1, 2],
         "constraints": ["不要修改实验结果"],
         "feedback": "第 3 条先不动"}
    """

    log = _logger(logger)
    findings = state.get("audit_findings") or []

    request = {
        "type": "human_approval",
        "run_id": state.get("run_id", "adhoc"),
        "iteration": int(state.get("iteration", 0)),
        "audit_meta": state.get("audit_meta") or {},
        "issues": [
            {
                "id": issue.get("id"),
                "category": issue.get("category"),
                "severity": issue.get("severity"),
                "location": issue.get("location"),
                "problem": issue.get("problem"),
                "suggestion": issue.get("suggestion"),
            }
            for issue in findings
        ],
        "instruction": "请选择需要修改的问题，并给出修改约束（例如：不要改变实验结果）。",
        "defaults": {
            "decision": "approve",
            "approved_issue_ids": [issue.get("id") for issue in findings],
            "constraints": [],
            "feedback": "",
        },
    }

    payload = interrupt(request)  # 首次执行到这里会暂停，恢复时返回用户输入
    update = normalize_human_payload(payload, findings)
    log(
        f"[Human] 用户确认修改 {len(update.get('approved_issue_ids') or [])} 个问题，"
        f"约束 {len(update.get('human_constraints') or [])} 条。"
    )

    return {
        **update,
        "trace": [
            _event(
                state,
                "human_approval",
                approved=len(update.get("approved_issue_ids") or []),
                decision=update.get("human_decision"),
            )
        ],
    }


def revision_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """一次提出本轮全部修改补丁（不直接改文档）。"""

    log = _logger(logger)
    try:
        update = revision_agent.run(state, recorder=_recorder(state), logger=log)
    except Exception as exc:  # noqa: BLE001 - 提补丁失败时保留原文，交给 Review 判断
        log(f"[Revision] 提出补丁异常：{type(exc).__name__}: {exc}")
        update = {
            "proposed_patches": [],
            "patch_stats": {},
            "revision_summary": f"本轮提出补丁失败：{exc}",
            "errors": [f"Revision 提出补丁异常：{type(exc).__name__}: {exc}"],
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


def issue_review_node(
    state: Any, *, logger: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """
    Audit 之后的问题闸门：逐条列出问题，问用户"有没有要忽略的"。

    用户的决定会**持久存进 user_rules**（kind=ignore，带指纹），
    因此下一轮重新审查时不会再问同一批问题，除非用户自己改主意。

    恢复时传入的 payload 支持：

        {"ignore_ids": [2, 4]}            # 这几条不改
        {"ignore_all": true}              # 全部不改
        {"approved_issue_ids": [1, 3]}    # 兼容旧写法：只改这些
        {"rules": ["全文统一把「土豆」写成「马铃薯」"]}   # 顺手加要求
        {"revoke": ["R1"]} / {"update": {"R1": "改成……"}}
    """

    log = _logger(logger)
    findings = state.get("audit_findings") or []
    rules = list(state.get("user_rules") or [])
    pending = [issue for issue in findings if not issue.get("ignored")]
    remembered = [
        rule
        for rule in rules
        if rule.get("kind") == "ignore" and rule.get("status", "active") == "active"
    ]

    if not pending:
        log("[Human] 所有问题都落在你已经确认“不改”的范围内，跳过这一步。")
        return {
            "trace": [_event(state, "issue_review", skipped=True, pending=0)],
        }

    request = {
        "type": "issue_review",
        "run_id": state.get("run_id", "adhoc"),
        "turn": int(state.get("turn", 1)),
        "iteration": int(state.get("iteration", 0)),
        "issues": [_public_issue(issue) for issue in pending],
        "remembered": [
            {"id": rule.get("id"), "text": rule.get("text")} for rule in remembered
        ],
        "rules": [
            {"id": rule.get("id"), "text": rule.get("text")}
            for rule in rules
            if rule.get("kind") == "rule" and rule.get("status", "active") == "active"
        ],
        "instruction": (
            "以下问题里有没有你不想改的？（默认都要改，直接回车继续）"
            "也可以顺手写要求，例如“全文统一把土豆写成马铃薯”。"
        ),
        "defaults": {"ignore_ids": [], "rules": []},
    }

    payload = interrupt(request)
    update = normalize_issue_payload(
        payload, findings, rules=rules, turn=int(state.get("turn", 1))
    )
    ignored_ids = [
        issue.get("id") for issue in update.get("audit_findings") or [] if issue.get("ignored")
    ]
    log(
        f"[Human] 已忽略 {len(ignored_ids)} 个问题；"
        f"当前共有 {len(update.get('user_rules') or [])} 条要求/决定。"
    )

    return {
        **update,
        "trace": [
            _event(
                state,
                "issue_review",
                ignored=len(ignored_ids),
                rules=len(update.get("user_rules") or []),
            )
        ],
    }


def normalize_issue_payload(
    payload: Any,
    findings: list[dict[str, Any]],
    *,
    rules: list[dict[str, Any]] | None = None,
    turn: int = 1,
) -> dict[str, Any]:
    """规范化问题闸门的输入，并把决定写进 user_rules。"""

    payload = payload if isinstance(payload, dict) else {}
    updated_findings = [dict(issue) for issue in findings]

    # 兼容旧写法：{"approved_issue_ids": [1, 3]} 表示“只改这些”
    if "approved_issue_ids" in payload:
        wanted = _as_id_set(payload.get("approved_issue_ids"))
        payload = {
            **payload,
            "ignore_ids": [
                issue.get("id")
                for issue in updated_findings
                if not issue.get("ignored") and str(issue.get("id")) not in wanted
            ],
        }

    new_rules, created = apply_rules_payload(
        list(rules or []),
        payload,
        turn=turn,
        source="issue_review",
        issues=updated_findings,
    )
    ignored_ids = [issue.get("id") for issue in updated_findings if issue.get("ignored")]

    return {
        "user_rules": new_rules,
        "human_constraints": rule_texts(new_rules),
        "audit_findings": updated_findings,
        "rules_added": created,
        "approved_issue_ids": [
            issue.get("id") for issue in updated_findings if not issue.get("ignored")
        ],
        "human_decision": "approve",
        "human_rounds": [
            {
                "type": "issue_review",
                "turn": turn,
                "ignored": ignored_ids,
                "approved": [
                    issue.get("id") for issue in updated_findings if not issue.get("ignored")
                ],
                "added_rules": [rule.get("id") for rule in created],
                "timestamp": utc_now(),
            }
        ],
    }


ISSUE_FIELDS = (
    "id",
    "span_label",
    "category",
    "severity",
    "location",
    "problem",
    "evidence",
    "suggestion",
    "ignored",
    "warning",
)


def _public_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {key: issue.get(key) for key in ISSUE_FIELDS if issue.get(key) is not None}


def patch_review_node(
    state: Any, *, logger: Callable[[str], None] | None = None
) -> dict[str, Any]:
    """
    补丁审阅闸门：把本轮**全部**改动一次性交给用户逐条接受 / 拒绝。

    这是默认流程里唯一必需的人工节点（问题闸门默认关闭），
    因此用户只会被问一次：看看这些改动，勾选你要的。

    恢复时传入的 payload 形如：

        {"accept_all": True}                      # 全部接受（默认）
        {"accepted_ids": ["patch-001", "patch-003"]}   # 只接受这几条
        {"rejected_ids": ["patch-002"]}                # 只拒绝这一条
        {"decisions": [{"patch_id": "patch-001", "decision": "reject"}]}
    """

    log = _logger(logger)
    patches = state.get("proposed_patches") or []
    decidable = [patch for patch in patches if patch.get("status") == STATUS_PROPOSED]

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
            "请勾选要接受的改动（默认全选）。"
            "被拒绝的改动不会写进论文，也不需要你写理由。"
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
    accepted = [patch for patch in patches if patch.status == STATUS_ACCEPTED]
    rejected = [patch for patch in patches if patch.status == STATUS_REJECTED]
    conflicts = [patch for patch in patches if patch.status == STATUS_CONFLICT]

    if not document.strip() or not accepted:
        summary = (
            f"本轮没有需要应用的改动（拒绝 {len(rejected)} 条，"
            f"无法定位 {len(conflicts)} 条）。"
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
                "summary": f"Review 复核异常：{exc}",
                "remaining_issues": [],
                "constraint_violations": [],
                "dimension_scores": {},
                "confidence": 0.0,
                "error": str(exc),
            },
            "errors": [f"Review 复核异常：{type(exc).__name__}: {exc}"],
        }

    status = (update.get("review_result") or {}).get("status", "fail")
    return {
        **update,
        "trace": [_event(state, "review", ok=status == "pass", status=status)],
    }


def report_node(state: Any, *, logger: Callable[[str], None] | None = None) -> dict[str, Any]:
    """汇总报告并落盘。"""

    log = _logger(logger)
    run_id = state.get("run_id") or "adhoc"
    trace_summary = get_recorder(run_id).summary()

    try:
        update = report_agent.write_outputs(state, trace_summary=trace_summary, logger=log)
    except Exception as exc:  # noqa: BLE001
        log(f"[Report] 生成报告失败：{type(exc).__name__}: {exc}")
        update = {"final_report": "", "errors": [f"生成报告失败：{exc}"]}

    return {
        **update,
        "trace": [_event(state, "report", finished_at=utc_now())],
    }


# ---------------------------------------------------------------- 工具函数


def normalize_human_payload(
    payload: Any, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    规范化人工确认的输入，容忍多种写法。

    支持：

    - approved_issue_ids: [1, 2] / ["all"] / ["none"] / 1 / "1,2"
    - constraints: ["..."] 或 "用分号/换行分隔的字符串"
    - decision: approve / reject（默认 approve）
    """

    all_ids = [issue.get("id") for issue in findings if issue.get("id") is not None]
    payload = payload if isinstance(payload, dict) else {}

    decision = str(payload.get("decision") or "approve").strip().lower()
    if decision not in {"approve", "reject"}:
        decision = "approve"

    approved_ids = _parse_ids(payload.get("approved_issue_ids"), all_ids)

    raw_constraints = payload.get("constraints")
    if raw_constraints is None:
        constraints: list[str] = []
    elif isinstance(raw_constraints, str):
        pieces = raw_constraints.replace("；", "\n").replace(";", "\n").split("\n")
        constraints = [piece.strip(" -•\t") for piece in pieces]
        constraints = [piece for piece in constraints if piece]
    else:
        constraints = [str(item).strip() for item in raw_constraints if str(item).strip()]

    feedback = str(payload.get("feedback") or "").strip()

    approved_set = set(approved_ids)
    updated_findings: list[dict[str, Any]] = []
    for issue in findings:
        item = dict(issue)
        item["approved"] = item.get("id") in approved_set
        updated_findings.append(item)

    return {
        "approved_issue_ids": approved_ids,
        "human_constraints": constraints,
        "human_feedback": feedback,
        "human_decision": decision,
        "audit_findings": updated_findings,
        "human_rounds": [
            {
                "iteration": payload.get("iteration"),
                "decision": decision,
                "approved": approved_ids,
                "constraints": constraints,
                "feedback": feedback,
                "timestamp": utc_now(),
            }
        ],
    }


def _parse_ids(raw_ids: Any, all_ids: list[Any]) -> list[int]:
    """把各种写法的问题编号统一成 int 列表。"""

    if raw_ids is None:
        return [int(issue_id) for issue_id in all_ids]

    if isinstance(raw_ids, str):
        text = raw_ids.strip().lower()
        if text in {"all", "全部", "*", ""}:
            return [int(issue_id) for issue_id in all_ids]
        if text in {"none", "无", "不修改"}:
            return []
        candidates: list[Any] = text.replace("，", ",").split(",")
    elif isinstance(raw_ids, (int, float)):
        candidates = [raw_ids]
    else:
        candidates = list(raw_ids or [])

    parsed: list[int] = []
    for item in candidates:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            parsed.append(int(item))
        elif isinstance(item, str) and item.strip().isdigit():
            parsed.append(int(item.strip()))

    allowed = {int(issue_id) for issue_id in all_ids}
    return [issue_id for issue_id in parsed if issue_id in allowed]


PATCH_FIELDS = (
    "patch_id",
    "issue_id",
    "span_id",
    "span_label",
    "sentence_ref",
    "position",
    "status",
    "status_label",
    "before",
    "replace",
    "rationale",
    "category",
    "severity",
    "conflict_reason",
    "warnings",
    "round",
    "decided_by",
    "decision_note",
)


def _public_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """给前端 / 命令行看的补丁视图（不含内部定位偏移）。"""

    return {key: patch.get(key) for key in PATCH_FIELDS if patch.get(key) is not None}


def normalize_patch_payload(
    payload: Any, patches: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    把用户的补丁决定规范化。

    默认是**全部接受**（界面上复选框默认勾选），
    用户只需要取消掉不想要的几条，不用写理由。
    """

    payload = payload if isinstance(payload, dict) else {}
    decidable = [
        patch for patch in patches if patch.get("status") == STATUS_PROPOSED
    ]
    decidable_ids = [str(patch.get("patch_id")) for patch in decidable]

    # 1) 逐条 decisions（最灵活）
    decision_map: dict[str, str] = {}
    raw_decisions = payload.get("decisions")
    if isinstance(raw_decisions, dict):
        for patch_id, value in raw_decisions.items():
            decision = value.get("decision") if isinstance(value, dict) else value
            decision_map[str(patch_id)] = str(decision or "").strip().lower()
    elif isinstance(raw_decisions, list):
        for item in raw_decisions:
            if isinstance(item, dict) and item.get("patch_id"):
                decision_map[str(item["patch_id"])] = str(
                    item.get("decision") or "accept"
                ).strip().lower()
            elif isinstance(item, str):
                decision_map[item] = "accept"

    # 2) 白名单 / 黑名单
    accepted_ids = payload.get("accepted_ids")
    rejected_ids = payload.get("rejected_ids")
    if not decision_map and isinstance(accepted_ids, (list, tuple, set, str)):
        wanted = _as_id_set(accepted_ids)
        decision_map = {
            patch_id: ("accept" if patch_id in wanted else "reject")
            for patch_id in decidable_ids
        }
    elif not decision_map and isinstance(rejected_ids, (list, tuple, set, str)):
        unwanted = _as_id_set(rejected_ids)
        decision_map = {
            patch_id: ("reject" if patch_id in unwanted else "accept")
            for patch_id in decidable_ids
        }

    # 3) 全接受 / 全拒绝；都没给就默认全接受
    if not decision_map:
        default = "reject" if payload.get("reject_all") else "accept"
        decision_map = {patch_id: default for patch_id in decidable_ids}

    note = str(payload.get("note") or payload.get("feedback") or "").strip()
    now = utc_now()
    updated: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for patch in patches:
        item = dict(patch)
        patch_id = str(item.get("patch_id"))
        if item.get("status") == STATUS_PROPOSED:
            decision = decision_map.get(patch_id, "accept")
            if decision not in {"accept", "reject"}:
                decision = "accept"
            if decision == "accept":
                item["status"] = STATUS_ACCEPTED
                accepted.append(item)
            else:
                item["status"] = STATUS_REJECTED
                rejected.append(item)
            item["decided_by"] = "user"
            item["decided_at"] = now
            if note:
                item["decision_note"] = note
        elif item.get("status") == STATUS_REJECTED:
            rejected.append(item)
        elif item.get("status") == STATUS_ACCEPTED:
            accepted.append(item)
        updated.append(item)

    rejected_memory = [
        {
            "patch_id": patch.get("patch_id"),
            "span_id": patch.get("span_id"),
            "span_label": patch.get("span_label"),
            "before": patch.get("before"),
            "replace": patch.get("replace"),
            "rationale": patch.get("rationale"),
            "note": patch.get("decision_note", ""),
            "turn": payload.get("turn"),
            "round": patch.get("round"),
            "at": now,
        }
        for patch in rejected
        if patch.get("decided_by") == "user"
    ]

    stats = {
        "proposed": sum(1 for patch in updated if patch.get("status") == STATUS_PROPOSED),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "applied": 0,
        "conflict": sum(1 for patch in updated if patch.get("status") == STATUS_CONFLICT),
    }

    return {
        "proposed_patches": updated,
        "patch_decisions": {
            patch.get("patch_id"): {
                "decision": "accept"
                if patch.get("status") == STATUS_ACCEPTED
                else "reject",
                "note": patch.get("decision_note", ""),
                "decided_by": patch.get("decided_by", "user"),
                "at": patch.get("decided_at", now),
            }
            for patch in updated
            if patch.get("status") in {STATUS_ACCEPTED, STATUS_REJECTED}
        },
        "patch_stats": stats,
        "rejected_patches": [patch for patch in rejected if patch.get("decided_by") == "user"],
        "rejected_memory": rejected_memory,
        "human_rounds": [
            {
                "type": "patch_review",
                "turn": payload.get("turn"),
                "accepted": [patch.get("patch_id") for patch in accepted],
                "rejected": [patch.get("patch_id") for patch in rejected],
                "note": note,
                "timestamp": now,
            }
        ],
    }


def _as_id_set(value: Any) -> set[str]:
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
        return {part.strip() for part in parts if part.strip()}
    return {str(item).strip() for item in value if str(item).strip()}
