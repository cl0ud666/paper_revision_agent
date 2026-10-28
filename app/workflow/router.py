"""
Workflow Router（条件边）。

Router不执行任务，只根据State判断“下一步去哪个Node”。

关键规则：

1. Orchestrator决定首次进入哪个Node；
2. Audit失败或没有发现问题 → 直接出报告；
3. 用户没有批准任何问题 → 直接出报告；
4. Review PASS → 报告；FAIL → 回到Orchestrator重新规划；
5. 达到迭代上限后强制出报告（熔断，避免无限循环）。
"""

from __future__ import annotations

from typing import Any

from app.state.schema import ROUTE_ACTIONS


def _explicit_work(state):
    from app.document.requests import explicit_targets
    from app.document.spans import build_spans

    return (
        not state.get("iteration")
        and not (state.get("budget") or {}).get("exceeded")
        and bool(explicit_targets(state, build_spans(state.get("current_document") or "")))
    )


def route_from_orchestrator(state: Any) -> str:
    """根据Orchestrator的规划决定下一个Node。"""

    from app.agents.orchestrator import requires_initial_audit

    if requires_initial_audit(state):
        return "audit"
    action = str(state.get("next_action") or "").strip()
    if action not in ROUTE_ACTIONS:
        # 状态异常时回到最安全的路径：先审查
        return "audit"
    # 已经问过用户一轮"前后矛盾"了，就不再开第二次闸门——
    # 同一批矛盾反复问只会让人烦；用户的判断已经作为规则存在State里，
    # Revision会照它执行。真要再问，请让用户在多轮追问里自己提。
    if action == "human_approval" and state.get("decisions"):
        return "revision"
    return "revision" if action == "report" and _explicit_work(state) else action


def route_after_audit(state: Any) -> str:
    """
    审查完成后：

    - 审查失败 / 没有发现问题 → 直接出报告；
    - **只有"前后矛盾"这类必须由作者拍板的问题**才停下来问用户；
    - 其它问题（写错的、可以更好的）不问，直接去提补丁。

    为什么不再问"有没有要忽略的"：论文修改里绝大多数问题没有歧义，
    让用户逐条过一遍纯属打扰；真正需要人拍板的只有"以哪一处为准"这种。
    """

    if state.get("audit_failed"):
        return "report"
    if not state.get("audit_findings"):
        return "revision" if _explicit_work(state) else "report"
    if _pending_decisions(state):
        return "issue_review"
    return "revision"


def _pending_decisions(state: Any) -> list[dict[str, Any]]:
    """还没被用户拍板的"需要在冲突里选一个"的问题。"""

    if state.get("decisions"):
        return []  # 已经拍过板了，不再重复打扰
    return [
        issue
        for issue in (state.get("audit_findings") or [])
        if (issue.get("kind") or "fix") == "decision" and not issue.get("ignored")
    ]


def route_after_issue_review(state: Any) -> str:
    """决策闸门之后：还有要改的问题就进入修改，全被忽略就直接出报告。"""

    findings = state.get("audit_findings") or []
    pending = [issue for issue in findings if not issue.get("ignored")]
    return "revision" if pending or _explicit_work(state) else "report"


def route_after_human(state: Any) -> str:
    """人工确认后：批准了问题就修改，否则直接出报告。"""

    decision = str(state.get("human_decision") or "approve").lower()
    if decision == "reject":
        return "report"
    if not state.get("approved_issue_ids"):
        return "report"
    return "revision"


def route_after_review(state: Any) -> str:
    """
    Review之后：PASS出报告；FAIL回Orchestrator重新规划。

    注意：是否达标由Review决定，Orchestrator只负责“接下来怎么办”。
    """

    result = state.get("review_result") or {}
    status = str(result.get("status") or "fail").lower()

    if status == "pass":
        return "report"

    # 预算耗尽时不再循环，直接收敛出报告（避免继续烧钱）
    if result.get("budget_exhausted") or (state.get("budget") or {}).get("exceeded"):
        return "report"

    iteration = int(state.get("iteration", 0))
    max_iterations = int(state.get("max_iterations", 3))
    if iteration >= max_iterations:
        # 熔断：不再无限重试，输出当前最优结果
        return "report"

    return "orchestrator"


def route_after_patch_review(state: Any) -> str:
    """补丁审阅之后：有被接受的补丁就应用，一条都没接受就直接出报告。"""

    patches = state.get("proposed_patches") or []
    accepted = [patch for patch in patches if patch.get("status") == "accepted"]
    return "apply" if accepted else "report"
