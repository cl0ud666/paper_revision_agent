"""
Orchestrator Agent：初始规划 + Review FAIL 后的自适应重规划。

它只在两个时间点被调用：

1. **Initial Planning**：任务开始时判断执行路径（全文审查 / 直接修改）；
2. **Adaptive Re-planning**：Review 判定 FAIL 时，基于当前 State 重新决定下一步。

因此正常路径是稳定的固定流程，只有异常情况才会重新唤起 Planner，
避免每一步都调用高成本模型。

LLM 不可用时，会退化为确定性的规则规划（保证 Workflow 不会因为一次
规划失败而整体崩掉）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agents.prompts import ORCHESTRATOR_SYSTEM, render_orchestrator_prompt
from app.config import get_settings
from app.state.schema import ISSUE_CATEGORIES, ROUTE_ACTIONS
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard

# 命中这些词 -> 用户希望“全面检查”
FULL_REVIEW_KEYWORDS = (
    "全面",
    "整体",
    "全文",
    "审查",
    "检查",
    "审阅",
    "校订",
    "full review",
    "review",
    "audit",
    "proofread",
)

# 命中这些词 -> 用户已经给出明确的局部修改要求
DIRECT_REVISION_HINTS = (
    "只修改",
    "只改",
    "仅修改",
    "仅需修改",
    "只需要修改",
    "不用审查",
    "不需要审查",
    "无需审查",
    "直接修改",
    "只需修改",
)

DEFAULT_FOCUS_AREAS = tuple(ISSUE_CATEGORIES[:6])


def plan(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    进行一次规划，返回需要写回 State 的字段。
    """

    log = logger or print
    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
    review_result = state.get("review_result")
    is_replan = bool(review_result) and iteration > 0

    context_text = format_context(build_context(state, "orchestrator"))
    prompt = render_orchestrator_prompt(
        context_text,
        is_replan=is_replan,
        max_iterations=int(state.get("max_iterations", settings.workflow.max_iterations)),
    )

    guard = get_guard("orchestrator", on_event=log)
    try:
        response = guard.run(
            lambda: call_llm(
                prompt,
                "orchestrator",
                system=ORCHESTRATOR_SYSTEM,
                json_mode=True,
                run_id=run_id,
                action="replan" if is_replan else "initial_plan",
            ),
            action="replan" if is_replan else "initial_plan",
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
        )
        data = parse_json_object(
            response.text, required_keys=("next_action",), context="Orchestrator"
        )
        return _normalize(data, state, is_replan=is_replan, log=log)
    except Exception as exc:  # noqa: BLE001 - 规划失败必须降级而不是崩溃
        log(f"[Orchestrator] LLM 规划不可用（{type(exc).__name__}: {exc}），改用规则规划。")
        result = fallback_plan(state)
        result["plan_reason"] = f"{result['plan_reason']}（LLM 规划不可用：{exc}）"
        return result


def _as_str_list(value: Any, *, limit: int = 12) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [str(value)]
    return [str(item).strip() for item in items if str(item).strip()][:limit]


def _normalize(
    data: dict[str, Any],
    state: Any,
    *,
    is_replan: bool,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """校验并规范化 LLM 返回的规划结果。"""

    next_action = str(data.get("next_action", "")).strip().lower()
    if next_action not in ROUTE_ACTIONS:
        log(f"[Orchestrator] 返回了非法动作 {next_action!r}，改用规则规划。")
        return fallback_plan(state)

    has_document = bool((state.get("current_document") or "").strip())
    if not has_document:
        next_action = "report"

    plan_kind = str(data.get("plan_kind") or "").strip()
    if plan_kind not in {"full_review", "direct_revision", "replan"}:
        if is_replan:
            plan_kind = "replan"
        elif next_action == "audit":
            plan_kind = "full_review"
        else:
            plan_kind = "direct_revision"

    focus_areas = _as_str_list(data.get("focus_areas"))
    plan_steps = _as_str_list(data.get("plan"))
    if not plan_steps:
        plan_steps = _default_steps(next_action)

    return {
        "plan_kind": plan_kind,
        "next_action": next_action,
        "plan": plan_steps,
        "plan_reason": str(data.get("reason") or "").strip(),
        "focus_areas": focus_areas or list(DEFAULT_FOCUS_AREAS),
        "next_instructions": str(data.get("instructions") or "").strip(),
    }


def _default_steps(next_action: str) -> list[str]:
    steps = {
        "audit": ["审查论文并输出结构化问题清单", "交给用户确认修改范围"],
        "revision": ["在用户批准的问题与约束范围内执行修改"],
        "human_approval": ["把待确认问题交给用户决策"],
        "report": ["汇总本次任务结果并输出报告"],
    }
    return list(steps.get(next_action, []))


def fallback_plan(state: Any) -> dict[str, Any]:
    """
    确定性兜底规划（不依赖 LLM）。

    当模型不可用、输出非法或超时时使用，保证 Workflow 仍能推进。
    """

    iteration = int(state.get("iteration", 0))
    max_iterations = int(state.get("max_iterations", 3))
    document = (state.get("current_document") or "").strip()
    request = state.get("user_request") or ""
    request_lower = request.lower()
    review_result = state.get("review_result") or {}
    remaining = review_result.get("remaining_issues") or []
    audit_findings = state.get("audit_findings") or []
    approved = state.get("approved_issue_ids") or []

    if not document:
        return _result("report", "论文内容为空，无法执行修改。", ["直接输出报告"])

    # ---------- Review FAIL 之后的重新规划 ----------
    if review_result and iteration > 0:
        if iteration >= max_iterations:
            return _result(
                "report",
                f"已达到迭代上限（{max_iterations} 轮），输出当前最优结果。",
                ["输出当前最优结果与剩余问题"],
                kind="replan",
            )
        if remaining or approved:
            return _result(
                "revision",
                "Review 仍有未解决问题，继续执行修改。",
                ["针对 Review 的剩余问题再次修改", "重新交给 Review 复核"],
                kind="replan",
            )
        if state.get("audit_failed"):
            return _result(
                "report",
                "Audit 未能正常工作，无法继续，输出当前结果。",
                ["输出当前结果"],
                kind="replan",
            )
        return _result(
            "audit",
            "Review 无法给出可执行结论，重新做一次系统性审查。",
            ["重新审查论文"],
            kind="replan",
        )

    # ---------- 首轮规划 ----------
    if audit_findings and not approved:
        return _result(
            "human_approval",
            "已有审查结果，等待用户确认修改范围。",
            ["交给用户确认修改范围"],
        )

    wants_full_review = any(
        keyword in request or keyword in request_lower for keyword in FULL_REVIEW_KEYWORDS
    )
    wants_direct = any(hint in request for hint in DIRECT_REVISION_HINTS)

    if wants_full_review and not wants_direct:
        return _result(
            "audit",
            "用户要求全面检查论文，先执行系统性审查。",
            ["审查论文并输出结构化问题清单", "交给用户确认修改范围"],
            kind="full_review",
        )

    return _result(
        "revision",
        "用户已给出明确的修改要求，直接执行修改。",
        ["按用户要求直接修改", "交给 Review 复核"],
        kind="direct_revision",
    )


def _result(
    next_action: str,
    reason: str,
    steps: list[str],
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    return {
        "plan_kind": kind
        or ("full_review" if next_action == "audit" else "direct_revision"),
        "next_action": next_action,
        "plan": steps,
        "plan_reason": reason,
        "focus_areas": list(DEFAULT_FOCUS_AREAS),
        "next_instructions": "",
    }
