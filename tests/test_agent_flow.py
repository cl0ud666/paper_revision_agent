"""
Agent流程测试：补丁闸门、多轮会话、成本预算、逐条决定的规范化。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents.prompts import (
    AUDIT_HEADER,
    ORCHESTRATOR_HEADER,
    REVIEW_HEADER,
    REVISION_HEADER,
)
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import reset_checkpointers
from app.workflow.harness import Harness
from app.workflow.nodes import normalize_patch_payload
from tests.fakes import (
    audit_response,
    fake_factory,
    patch_response,
    plan_response,
    review_response,
)

DOC = """# 测试论文

## 摘要

本文提出了一个新的方法，效果比较好。

## 1 引言

图神经网络在节点分类任务上被广泛使用。
"""

ABSTRACT_SENTENCE = "本文提出了一个新的方法，效果比较好。"
ABSTRACT_IMPROVED = "本文提出了一种面向小样本场景的图神经网络方法，性能优于基线方法。"
INTRO_SENTENCE = "图神经网络在节点分类任务上被广泛使用。"
INTRO_IMPROVED = "图神经网络已被广泛用于节点分类任务。"


class AgentFlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._original_pra_env = {
            key: os.environ.get(key)
            for key in (
                "PRA_DATA_DIR",
                "PRA_RUNS_DIR",
                "PRA_CHECKPOINT_DB",
                "PRA_RETRY_MAX_ATTEMPTS",
                "PRA_RETRY_BASE_DELAY",
                "PRA_PATCH_GATE",
                "PRA_FINAL_CONFIRM",
                "PRA_BUDGET_RUN_TOKENS",
                "PRA_BUDGET_TURN_TOKENS",
            )
        }
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-flow-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_CHECKPOINT_DB"] = str(self.tmp_dir / "checkpoints" / "test.sqlite")
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        os.environ["PRA_RETRY_BASE_DELAY"] = "0"
        # 这个文件验证的是**交互式**流程，所以打开补丁闸门；
        # decision findings仍由工作流按问题类型进入人工拍板。
        os.environ["PRA_PATCH_GATE"] = "1"
        os.environ["PRA_FINAL_CONFIRM"] = "0"
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()

    def tearDown(self) -> None:
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        for key, value in self._original_pra_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def install(self, responses: dict) -> None:
        set_llm_factory(fake_factory(responses))


class TestMultiTurn(AgentFlowTestCase):
    """④ 多轮会话：第二轮要记得第一轮说过什么、拒过什么。"""

    def test_follow_up_keeps_context_and_rejections(self) -> None:
        prompts: dict[str, list[str]] = {"revision": []}

        def revision(prompt: str) -> str:
            prompts["revision"].append(prompt)
            if len(prompts["revision"]) == 1:
                return patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ABSTRACT_SENTENCE,
                            "replace": ABSTRACT_IMPROVED,
                            "rationale": "把笼统的结论改具体",
                        }
                    ]
                )
            return patch_response(
                [
                    {
                        "span_label": "P02",
                        "quote": INTRO_SENTENCE,
                        "replace": INTRO_IMPROVED,
                        "rationale": "按用户本轮要求，把引言的表述改得更学术",
                    }
                ]
            )

        def orchestrator(prompt: str) -> str:
            # 第二轮用户只说“改引言”，因此直接进入补丁阶段
            return plan_response("revision" if "第 2 轮" in prompt else "audit")

        self.install(
            {
                ORCHESTRATOR_HEADER: orchestrator,
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "语言表达",
                            "severity": "medium",
                            "problem": "结论表述笼统",
                            "evidence": ABSTRACT_SENTENCE,
                            "suggestion": "改成具体结论",
                        }
                    ]
                ),
                REVISION_HEADER: revision,
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(logger=lambda message: None)

        # ---------- 第一轮：提出补丁，但用户全部拒绝 ----------
        result = harness.start("请全面检查并修改这篇论文。", DOC)
        self.assertTrue(result.is_interrupted)
        self.assertEqual(result.pending_request["type"], "patch_review")
        result = harness.resume({"reject_all": True})

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state["applied_patches"], [])
        self.assertEqual(len(result.state["rejected_memory"]), 1)
        self.assertEqual(result.revised_document, DOC)  # 拒绝的改动不能落盘
        self.assertEqual(result.state["turn"], 1)

        # ---------- 第二轮：新要求 ----------
        result = harness.follow_up("把引言的表述改得更学术")

        self.assertTrue(result.is_interrupted)
        request = result.pending_request
        self.assertEqual(request["type"], "patch_review")
        self.assertEqual(request["turn"], 2)
        self.assertEqual(len(request["patches"]), 1)
        self.assertEqual(request["patches"][0]["span_label"], "P02")
        self.assertIn("引言", request["patches"][0]["rationale"])

        # 第二轮提补丁时，Prompt里带着第一轮被拒绝的改动（不会重复打扰）
        second_prompt = prompts["revision"][-1]
        self.assertIn("已被用户拒绝的改动", second_prompt)
        self.assertIn(ABSTRACT_IMPROVED[:12], second_prompt)
        self.assertIn("第 2 轮", second_prompt)

        result = harness.resume({"accept_all": True})

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state["turn"], 2)
        self.assertEqual([entry["turn"] for entry in result.state["turns"]], [1, 2])
        self.assertEqual(len(result.state["conversation"]), 2)
        # 第二轮接受了改动，第一轮被拒的改动仍然没有落盘
        self.assertIn(INTRO_IMPROVED, result.revised_document)
        self.assertNotIn(ABSTRACT_IMPROVED, result.revised_document)
        self.assertEqual(len(result.state["applied_patches"]), 1)

    def test_user_rules_persist_across_turns_and_can_be_changed(self) -> None:
        """
        ③ 用户说的“点”存在user_rules里：

        - 开局可以带要求（--rule）；
        - Audit之后可以忽略某些问题，决定会被记住；
        - 下一轮不再重复问同一批问题；
        - 用户可以用“取消R1”这类话改掉自己之前的要求。
        """

        prompts: dict[str, list[str]] = {"audit": [], "revision": []}

        def audit(prompt: str) -> str:
            prompts["audit"].append(prompt)
            return audit_response(
                [
                    {
                        "span_label": "P01",
                        "category": "学术规范",
                        "severity": "medium",
                        "problem": "参考文献格式不统一，缺少出版信息与文献类型标识",
                        "evidence": ABSTRACT_SENTENCE,
                        "suggestion": "统一格式",
                    }
                ]
            )

        def revision(prompt: str) -> str:
            prompts["revision"].append(prompt)
            return patch_response(
                [
                    {
                        "span_label": "P02",
                        "quote": INTRO_SENTENCE,
                        "replace": INTRO_IMPROVED,
                        "rationale": "按用户要求把引言写具体",
                    }
                ]
            )

        def orchestrator(prompt: str) -> str:
            return plan_response("revision" if "第 2 轮" in prompt else "audit")

        self.install(
            {
                ORCHESTRATOR_HEADER: orchestrator,
                AUDIT_HEADER: audit,
                REVISION_HEADER: revision,
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(logger=lambda message: None)
        result = harness.start(
            "请全面检查并修改这篇论文。",
            DOC,
            rules=["全文统一把「土豆」写成「马铃薯」"],
        )

        # 开局的要求立刻可见，并且Audit也看得到
        self.assertIn("马铃薯", result.state["user_rules"][0]["text"])
        self.assertIn("马铃薯", prompts["audit"][0])

        # 唯一的闸门是补丁闸门；本轮全部接受
        self.assertEqual(result.pending_request["type"], "patch_review")
        result = harness.resume({"accept_all": True})
        self.assertEqual(result.status, "completed")

        rules = result.state["user_rules"]
        self.assertEqual([rule["kind"] for rule in rules], ["rule"])
        self.assertEqual(rules[0]["id"], "R1")

        # 第二轮：可以直接改要求（撤销旧要求 + 提新要求）
        result = harness.follow_up("取消 R1\n把引言写具体")
        self.assertTrue(result.is_interrupted)
        self.assertEqual(result.pending_request["type"], "patch_review")
        # 被撤销的要求不再出现在提示里；新的追问在里面
        self.assertNotIn("马铃薯", prompts["revision"][-1])
        self.assertIn("把引言写具体", prompts["revision"][-1])

        rules = result.state["user_rules"]
        self.assertEqual(rules[0]["status"], "revoked")
        self.assertEqual(result.state["user_request"], "把引言写具体")

        result = harness.resume({"accept_all": True})
        self.assertEqual(result.status, "completed")
        self.assertIn(INTRO_IMPROVED, result.revised_document)
        self.assertIn("用户要求与决定", result.final_report)
        self.assertIn("已撤销", result.final_report)


class TestPatchGate(AgentFlowTestCase):
    """② 逐条接受/拒绝：一次看全部，一次做决定。"""

    def test_partial_acceptance_applies_only_selected(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "语言表达",
                            "severity": "high",
                            "problem": "结论笼统",
                            "evidence": ABSTRACT_SENTENCE,
                            "suggestion": "改具体",
                        },
                        {
                            "span_label": "P02",
                            "category": "学术规范",
                            "severity": "low",
                            "problem": "引言表述不够学术",
                            "evidence": INTRO_SENTENCE,
                            "suggestion": "改学术",
                        },
                    ]
                ),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ABSTRACT_SENTENCE,
                            "replace": ABSTRACT_IMPROVED,
                            "rationale": "把笼统结论改具体",
                        },
                        {
                            "span_label": "P02",
                            "issue_id": 2,
                            "quote": INTRO_SENTENCE,
                            "replace": INTRO_IMPROVED,
                            "rationale": "术语表述更学术",
                        },
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(logger=lambda message: None)
        result = harness.start("请全面检查并修改这篇论文。", DOC)

        request = result.pending_request
        # 一次性给出全部改动，用户只被问一次
        self.assertEqual(len(request["patches"]), 2)
        self.assertEqual(request["decidable_ids"], ["patch-001", "patch-002"])

        # 只接受第二条：第一条应当既不落盘也不影响第二条
        result = harness.resume({"accepted_ids": ["patch-002"]})

        self.assertEqual(result.status, "completed")
        self.assertIn(INTRO_IMPROVED, result.revised_document)
        self.assertNotIn(ABSTRACT_IMPROVED, result.revised_document)
        stats = result.state["patch_stats"]
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["rejected"], 1)

    def test_rejected_patch_must_not_leak_into_document(self) -> None:
        """如果被拒绝的改动仍然出现在文本里，Review必须判fail。"""

        from app.agents import review as review_agent

        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        state = {
            "run_id": "leak-check",
            "user_request": "全面检查并修改",
            "original_document": DOC,
            "current_document": DOC.replace(ABSTRACT_SENTENCE, ABSTRACT_IMPROVED),
            "proposed_patches": [],
            "rejected_patches": [
                {
                    "patch_id": "patch-001",
                    "span_id": "p1",
                    "span_label": "P01",
                    "before": ABSTRACT_SENTENCE,
                    "replace": ABSTRACT_IMPROVED,
                    "rationale": "改具体",
                }
            ],
            "applied_patches": [],
            "iteration": 1,
            "max_iterations": 3,
            "revision_edits": [],
        }
        update = review_agent.run(state, logger=lambda message: None)

        self.assertEqual(update["review_result"]["status"], "fail")
        checks = {check["name"]: check["status"] for check in update["review_result"]["checks"]}
        self.assertEqual(checks["尊重用户决定"], "fail")

    def test_user_rejection_shrinks_scope_and_passes(self) -> None:
        """
        用户拒绝某条改动 = 主动缩小范围。

        Review不能因为“这条没改”就判fail，否则会一轮轮重复打扰用户。
        """

        from app.agents import review as review_agent

        set_llm_factory(
            fake_factory(
                {
                    REVIEW_HEADER: review_response(
                        "fail",
                        summary="还有问题没改",
                        remaining_issues=[
                            {
                                "id": 2,
                                "span_label": "P02",
                                "category": "学术规范",
                                "severity": "medium",
                                "problem": "引言表述不够学术",
                                "suggestion": "改学术",
                            },
                            {
                                "id": 9,
                                "span_label": "P09",
                                "category": "方法描述",
                                "severity": "low",
                                "problem": "其它无关问题",
                                "suggestion": "另说",
                            },
                        ],
                    )
                }
            )
        )
        state = {
            "run_id": "scope-check",
            "user_request": "全面检查并修改",
            "original_document": DOC,
            "current_document": DOC.replace(ABSTRACT_SENTENCE, ABSTRACT_IMPROVED),
            "audit_findings": [
                {"id": 1, "span_label": "P01", "problem": "结论笼统", "category": "语言表达"},
                {
                    "id": 2,
                    "span_label": "P02",
                    "problem": "引言表述不够学术",
                    "category": "学术规范",
                },
            ],
            "proposed_patches": [],
            "applied_patches": [
                {
                    "patch_id": "patch-001",
                    "issue_id": 1,
                    "span_label": "P01",
                    "before": ABSTRACT_SENTENCE,
                    "replace": ABSTRACT_IMPROVED,
                    "rationale": "改具体",
                    "status": "applied",
                }
            ],
            "rejected_patches": [
                {
                    "patch_id": "patch-002",
                    "issue_id": 2,
                    "span_label": "P02",
                    "before": INTRO_SENTENCE,
                    "replace": INTRO_IMPROVED,
                    "rationale": "改学术",
                    "status": "rejected",
                }
            ],
            "iteration": 1,
            "max_iterations": 3,
            "revision_edits": [],
        }

        update = review_agent.run(state, logger=lambda message: None)
        result = update["review_result"]

        # P02那条属于用户已拒绝的范围，被排除；只剩真正未解决的一条
        self.assertEqual(len(result["remaining_issues"]), 1)
        self.assertEqual(result["remaining_issues"][0]["span_label"], "P09")

        # 若“剩余问题”全部属于被拒范围，则改判通过
        state["rejected_patches"].append(
            {
                "patch_id": "patch-003",
                "issue_id": 3,
                "span_label": "P09",
                "before": "x",
                "replace": "y",
                "rationale": "无关问题",
                "status": "rejected",
            }
        )
        state["audit_findings"].append(
            {"id": 3, "span_label": "P09", "problem": "其它无关问题", "category": "方法描述"}
        )
        update = review_agent.run(state, logger=lambda message: None)
        self.assertEqual(update["review_result"]["status"], "pass")
        self.assertEqual(update["review_result"]["remaining_issues"], [])


class TestBudget(AgentFlowTestCase):
    """⑥ 成本与延迟预算：超预算要降级收尾，而不是继续烧钱。"""

    def test_exhausted_budget_degrades_to_report(self) -> None:
        os.environ["PRA_BUDGET_RUN_TOKENS"] = "1"
        reset_settings()
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "语言表达",
                            "severity": "high",
                            "problem": "结论笼统",
                            "evidence": ABSTRACT_SENTENCE,
                            "suggestion": "改具体",
                        }
                    ]
                ),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "quote": ABSTRACT_SENTENCE,
                            "replace": ABSTRACT_IMPROVED,
                            "rationale": "改具体",
                        }
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.state["budget"]["exceeded"])
        self.assertIn("预算", result.state["budget"]["reason"])
        # 降级后不再提补丁，直接出报告
        self.assertTrue(result.state["audit_meta"]["budget_stopped"])
        self.assertFalse(result.state["proposed_patches"])
        self.assertTrue(result.report_path)
        self.assertIn("成本与预算", result.final_report)

    def test_budget_snapshot_is_recorded(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response([]),
                REVIEW_HEADER: review_response("pass"),
            }
        )
        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        budget = result.state["budget"]
        self.assertGreater(budget["used_tokens"], 0)
        self.assertGreater(budget["calls_total"], 0)
        self.assertIn("calls_per_agent", budget)
        self.assertFalse(budget["exceeded"])


class TestPatchPayloadNormalization(unittest.TestCase):
    """人工输入的各种写法都要能正确解析（用户不该被格式问题绊住）。"""

    def setUp(self) -> None:
        self.patches = [
            {"patch_id": "patch-001", "status": "proposed", "span_label": "P01"},
            {"patch_id": "patch-002", "status": "proposed", "span_label": "P02"},
            {"patch_id": "patch-003", "status": "conflict", "span_label": "P03"},
        ]

    def test_default_accepts_all(self) -> None:
        update = normalize_patch_payload({}, self.patches)
        self.assertEqual(update["patch_stats"]["accepted"], 2)
        self.assertEqual(update["patch_stats"]["rejected"], 0)
        self.assertEqual(update["patch_stats"]["conflict"], 1)

    def test_accept_all_flag(self) -> None:
        update = normalize_patch_payload({"accept_all": True}, self.patches)
        self.assertEqual(update["patch_stats"]["accepted"], 2)

    def test_reject_all_flag(self) -> None:
        update = normalize_patch_payload({"reject_all": True}, self.patches)
        self.assertEqual(update["patch_stats"]["rejected"], 2)
        self.assertEqual(len(update["rejected_memory"]), 2)

    def test_accepted_ids_whitelist(self) -> None:
        update = normalize_patch_payload({"accepted_ids": ["patch-002"]}, self.patches)
        by_id = {patch["patch_id"]: patch["status"] for patch in update["proposed_patches"]}
        self.assertEqual(by_id["patch-001"], "rejected")
        self.assertEqual(by_id["patch-002"], "accepted")
        self.assertEqual(by_id["patch-003"], "conflict")  # 无法定位的不受影响

    def test_rejected_ids_blacklist(self) -> None:
        update = normalize_patch_payload({"rejected_ids": ["patch-001"]}, self.patches)
        by_id = {patch["patch_id"]: patch["status"] for patch in update["proposed_patches"]}
        self.assertEqual(by_id["patch-001"], "rejected")
        self.assertEqual(by_id["patch-002"], "accepted")

    def test_comma_separated_string(self) -> None:
        update = normalize_patch_payload({"accepted_ids": "patch-001, patch-002"}, self.patches)
        self.assertEqual(update["patch_stats"]["accepted"], 2)

    def test_decisions_list(self) -> None:
        update = normalize_patch_payload(
            {"decisions": [{"patch_id": "patch-001", "decision": "reject"}]}, self.patches
        )
        by_id = {patch["patch_id"]: patch["status"] for patch in update["proposed_patches"]}
        self.assertEqual(by_id["patch-001"], "rejected")
        self.assertEqual(by_id["patch-002"], "accepted")
        self.assertEqual(len(update["rejected_memory"]), 1)
        self.assertEqual(update["rejected_memory"][0]["patch_id"], "patch-001")

    def test_human_rounds_recorded(self) -> None:
        update = normalize_patch_payload(
            {"accepted_ids": ["patch-001"], "note": "第二条先不动", "turn": 2}, self.patches
        )
        self.assertEqual(len(update["human_rounds"]), 1)
        self.assertEqual(update["human_rounds"][0]["type"], "patch_review")
        self.assertEqual(update["human_rounds"][0]["turn"], 2)
        self.assertEqual(update["patch_decisions"]["patch-002"]["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
