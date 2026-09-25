"""
Router 单元测试：确认条件路由与迭代上限熔断符合设计。
"""

from __future__ import annotations

import unittest

from app.workflow.router import (
    route_after_audit,
    route_after_human,
    route_after_patch_review,
    route_after_review,
    route_from_orchestrator,
)
from app.workflow.nodes import normalize_human_payload

FINDINGS = [
    {"id": 1, "problem": "问题一"},
    {"id": 2, "problem": "问题二"},
    {"id": 3, "problem": "问题三"},
]


class TestHumanPayload(unittest.TestCase):
    """人工确认输入的规范化（容忍前端传来的各种写法）。"""

    def test_list_of_ids(self) -> None:
        update = normalize_human_payload({"approved_issue_ids": [1, 3]}, FINDINGS)
        self.assertEqual(update["approved_issue_ids"], [1, 3])
        self.assertEqual(update["human_decision"], "approve")
        approved = {issue["id"]: issue["approved"] for issue in update["audit_findings"]}
        self.assertTrue(approved[1])
        self.assertFalse(approved[2])

    def test_all_keyword(self) -> None:
        update = normalize_human_payload({"approved_issue_ids": "all"}, FINDINGS)
        self.assertEqual(update["approved_issue_ids"], [1, 2, 3])

    def test_none_keyword_and_reject(self) -> None:
        update = normalize_human_payload(
            {"decision": "reject", "approved_issue_ids": "none"}, FINDINGS
        )
        self.assertEqual(update["approved_issue_ids"], [])
        self.assertEqual(update["human_decision"], "reject")

    def test_comma_string_and_unknown_ids(self) -> None:
        update = normalize_human_payload({"approved_issue_ids": "1, 2, 99"}, FINDINGS)
        self.assertEqual(update["approved_issue_ids"], [1, 2])

    def test_default_is_all_when_missing(self) -> None:
        update = normalize_human_payload({}, FINDINGS)
        self.assertEqual(update["approved_issue_ids"], [1, 2, 3])

    def test_constraints_from_string(self) -> None:
        update = normalize_human_payload(
            {"constraints": "- 不要修改实验结果;\n- 保持作者风格；"}, FINDINGS
        )
        self.assertEqual(update["human_constraints"], ["不要修改实验结果", "保持作者风格"])

    def test_non_dict_payload(self) -> None:
        update = normalize_human_payload(None, FINDINGS)
        self.assertEqual(update["approved_issue_ids"], [1, 2, 3])


class TestInitialRouting(unittest.TestCase):
    def test_full_review_goes_to_audit(self) -> None:
        self.assertEqual(route_from_orchestrator({"next_action": "audit"}), "audit")

    def test_direct_revision_goes_to_revision(self) -> None:
        self.assertEqual(route_from_orchestrator({"next_action": "revision"}), "revision")

    def test_human_and_report(self) -> None:
        self.assertEqual(
            route_from_orchestrator({"next_action": "human_approval"}), "human_approval"
        )
        self.assertEqual(route_from_orchestrator({"next_action": "report"}), "report")

    def test_unknown_action_falls_back_to_audit(self) -> None:
        self.assertEqual(route_from_orchestrator({"next_action": "乱写"}), "audit")
        self.assertEqual(route_from_orchestrator({}), "audit")


class TestAuditRouting(unittest.TestCase):
    def test_issues_found_goes_straight_to_revision_by_default(self) -> None:
        """默认不做“问题闸门”：一次产出补丁，用户在补丁清单上决定。"""

        state = {"audit_findings": [{"id": 1}], "audit_failed": False}
        self.assertEqual(route_after_audit(state), "revision")

    def test_issue_gate_enabled_routes_to_issue_review(self) -> None:
        state = {"audit_findings": [{"id": 1}], "audit_failed": False, "issue_gate": True}
        self.assertEqual(route_after_audit(state), "issue_review")

    def test_issue_review_routing(self) -> None:
        from app.workflow.router import route_after_issue_review

        pending = {"audit_findings": [{"id": 1}, {"id": 2, "ignored": True}]}
        self.assertEqual(route_after_issue_review(pending), "revision")
        all_ignored = {"audit_findings": [{"id": 1, "ignored": True}]}
        self.assertEqual(route_after_issue_review(all_ignored), "report")

    def test_no_issues_goes_to_report(self) -> None:
        self.assertEqual(route_after_audit({"audit_findings": []}), "report")

    def test_audit_failed_goes_to_report(self) -> None:
        state = {"audit_findings": [{"id": 1}], "audit_failed": True}
        self.assertEqual(route_after_audit(state), "report")


class TestHumanRouting(unittest.TestCase):
    def test_approved_goes_to_revision(self) -> None:
        state = {"human_decision": "approve", "approved_issue_ids": [1, 2]}
        self.assertEqual(route_after_human(state), "revision")

    def test_reject_goes_to_report(self) -> None:
        state = {"human_decision": "reject", "approved_issue_ids": [1]}
        self.assertEqual(route_after_human(state), "report")

    def test_nothing_approved_goes_to_report(self) -> None:
        self.assertEqual(route_after_human({"approved_issue_ids": []}), "report")


class TestReviewRouting(unittest.TestCase):
    def test_pass_goes_to_report(self) -> None:
        state = {"review_result": {"status": "pass"}, "iteration": 1, "max_iterations": 3}
        self.assertEqual(route_after_review(state), "report")

    def test_fail_goes_to_orchestrator(self) -> None:
        state = {"review_result": {"status": "fail"}, "iteration": 1, "max_iterations": 3}
        self.assertEqual(route_after_review(state), "orchestrator")

    def test_fail_at_iteration_cap_goes_to_report(self) -> None:
        state = {"review_result": {"status": "fail"}, "iteration": 3, "max_iterations": 3}
        self.assertEqual(route_after_review(state), "report")

    def test_missing_result_is_treated_as_fail(self) -> None:
        state = {"iteration": 0, "max_iterations": 3}
        self.assertEqual(route_after_review(state), "orchestrator")

    def test_budget_exhausted_stops_looping(self) -> None:
        state = {
            "review_result": {"status": "fail", "budget_exhausted": True},
            "iteration": 1,
            "max_iterations": 3,
        }
        self.assertEqual(route_after_review(state), "report")


class TestPatchReviewRouting(unittest.TestCase):
    def test_accepted_patches_are_applied(self) -> None:
        state = {
            "proposed_patches": [
                {"patch_id": "patch-001", "status": "accepted"},
                {"patch_id": "patch-002", "status": "rejected"},
            ]
        }
        self.assertEqual(route_after_patch_review(state), "apply")

    def test_all_rejected_goes_to_report(self) -> None:
        state = {
            "proposed_patches": [
                {"patch_id": "patch-001", "status": "rejected"},
                {"patch_id": "patch-002", "status": "conflict"},
            ]
        }
        self.assertEqual(route_after_patch_review(state), "report")

    def test_no_patches_goes_to_report(self) -> None:
        self.assertEqual(route_after_patch_review({"proposed_patches": []}), "report")


if __name__ == "__main__":
    unittest.main()
