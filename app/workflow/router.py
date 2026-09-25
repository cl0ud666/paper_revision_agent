"""
Workflow Router（条件边）。

Router 不执行任务，只根据 State 判断“下一步去哪个 Node”。

关键规则：

1. Orchestrator 决定首次进入哪个 Node；
2. Audit 失败或没有发现问题 → 直接出报告；
3. 用户没有批准任何问题 → 直接出报告；
4. Review PASS → 报告；FAIL → 回到 Orchestrator 重新规划；
5. 达到迭代上限后强制出报告（熔断，避免无限循环）。
"""

from __future__ import annotations

from typing import Any

from app.state.schema import ROUTE_ACTIONS


def route_from_orchestrator(state: Any) -> str:
    """根据 Orchestrator 的规划决定下一个 Node。"""

    action = str(state.get("next_action") or "").strip()
    if action not in ROUTE_ACTIONS:
        # 状态异常时回到最安全的路径：先审查
        return "audit"
    return action


def route_after_audit(state: Any) -> str:
    """
    审查完成后：

    - 审查失败 / 没有发现问题 → 直接出报告；
    - 启用了问题闸门（PRA_ISSUE_GATE=1）→ 先让用户确认要改哪些问题；
    - 默认（闸门关闭）→ 直接去提补丁，用户只在补丁清单上做一次决定。
    """

    if state.get("audit_failed"):
        return "report"
    if not state.get("audit_findings"):
        return "report"
    if state.get("issue_gate"):
        # 先让用户看一遍问题清单：有没有要忽略的（默认都要改）
        return "issue_review"
    return "revision"


def route_after_issue_review(state: Any) -> str:
    """问题闸门之后：还有要改的问题就进入修改，全被忽略就直接出报告。"""

    findings = state.get("audit_findings") or []
    pending = [issue for issue in findings if not issue.get("ignored")]
    return "revision" if pending else "report"


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
    Review 之后：PASS 出报告；FAIL 回 Orchestrator 重新规划。

    注意：是否达标由 Review 决定，Orchestrator 只负责“接下来怎么办”。
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
