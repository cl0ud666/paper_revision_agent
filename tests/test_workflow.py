"""
端到端Workflow测试（使用假LLM，不联网）。

覆盖：

1. 正常路径：Audit → Human → Revision → Review PASS → Report
2. Human-in-the-loop：暂停（interrupt）后用用户输入恢复
3. Review FAIL后的自适应重规划与迭代上限熔断
4. Audit整体失败的异常隔离（降级出报告）
5. 没有发现问题时直接出报告
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
from app.workflow.harness import Harness
from tests.fakes import (
    audit_response,
    fake_factory,
    patch_response,
    plan_response,
    review_response,
)

DOC = """# 测试论文

## 摘要

本文提出了一个新的方法，效果比较好。实验结果表明我们的方法比基线方法好。

## 1 引言

图神经网络在节点分类任务上被广泛使用，但是小样本场景下的效果并不理想。
"""

ISSUE_SENTENCE = "本文提出了一个新的方法，效果比较好。"
IMPROVED_SENTENCE = "本文提出了一种面向小样本场景的图神经网络方法，性能优于基线方法。"
INTRO_SENTENCE = "图神经网络在节点分类任务上被广泛使用，但是小样本场景下的效果并不理想。"
INTRO_IMPROVED = "图神经网络虽然被广泛用于节点分类任务，但在标注样本很少的场景下性能会明显下降。"


class WorkflowTestCase(unittest.TestCase):
    """公共环境：临时data目录 + 假LLM。"""

    def setUp(self) -> None:
        self._original_pra_env = {
            key: os.environ.get(key)
            for key in (
                "PRA_RUNS_DIR",
                "PRA_DATA_DIR",
                "PRA_RETRY_MAX_ATTEMPTS",
                "PRA_RETRY_BASE_DELAY",
                "PRA_PATCH_GATE",
                "PRA_FINAL_CONFIRM",
            )
        }
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-test-"))
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        os.environ["PRA_RETRY_BASE_DELAY"] = "0"
        # 这个文件验证的是**交互式**流程，所以打开补丁闸门；
        # decision findings仍由工作流按问题类型进入人工拍板。
        os.environ["PRA_PATCH_GATE"] = "1"
        # 写Word前的那次确认对这里的断言没意义，关掉
        os.environ["PRA_FINAL_CONFIRM"] = "0"
        reset_settings()
        reset_guards()
        reset_recorders()

    def tearDown(self) -> None:
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        for key, value in self._original_pra_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def install(self, responses: dict) -> None:
        set_llm_factory(fake_factory(responses))


class TestHappyPath(WorkflowTestCase):
    def test_full_pipeline_passes(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "语言表达",
                            "severity": "high",
                            "location": "摘要",
                            "problem": "摘要中的结论表述过于笼统",
                            "evidence": ISSUE_SENTENCE,
                            "suggestion": "明确方法定位与对比结论",
                        }
                    ]
                ),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ISSUE_SENTENCE,
                            "replace": IMPROVED_SENTENCE,
                            "rationale": "把笼统的表述改成具体的方法定位与对比结论",
                        }
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.review_status, "pass")
        self.assertEqual(result.state["iteration"], 1)
        self.assertIn(IMPROVED_SENTENCE, result.revised_document)
        self.assertNotIn(ISSUE_SENTENCE, result.revised_document)
        self.assertEqual(len(result.state["applied_patches"]), 1)
        self.assertEqual(
            result.state["patch_stats"]["applied"] + result.state["patch_stats"]["accepted"], 1
        )
        self.assertTrue(result.report_path)
        self.assertTrue(Path(result.report_path).exists())
        self.assertTrue(Path(result.document_path).exists())
        self.assertIn("学术论文修改报告", result.final_report)

    def test_human_interrupt_then_resume(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "学术规范",
                            "severity": "medium",
                            "location": "方法",
                            "problem": "方法描述缺少实现细节",
                            "evidence": ISSUE_SENTENCE,
                            "suggestion": "补充实现细节",
                        }
                    ]
                ),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ISSUE_SENTENCE,
                            "replace": IMPROVED_SENTENCE,
                            "rationale": "补充细节",
                        }
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(logger=lambda message: None)
        result = harness.start("请全面检查并修改这篇论文。", DOC)

        # 唯一的闸门：补丁审阅（问题清单不再问"有没有要忽略的"，
        # 写错的问题直接进Revision，这里一次看完全部改动）
        self.assertTrue(result.is_interrupted)
        request = result.pending_request
        self.assertEqual(request["type"], "patch_review")
        self.assertEqual([patch["patch_id"] for patch in request["patches"]], ["patch-001"])
        self.assertEqual(request["patches"][0]["span_label"], "P01")
        self.assertTrue(request["patches"][0]["rationale"])

        # 用户只勾选接受第1条补丁（不需要写理由）
        result = harness.resume({"accepted_ids": ["patch-001"]})

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state["patch_stats"]["accepted"], 1)
        self.assertEqual(result.state["rejected_patches"], [])
        self.assertEqual(result.review_status, "pass")
        self.assertIn(IMPROVED_SENTENCE, result.revised_document)

    def test_rejecting_all_patches_goes_straight_to_report(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "语言表达",
                            "severity": "medium",
                            "problem": "表达略有冗余",
                            "evidence": ISSUE_SENTENCE,
                            "suggestion": "删减冗余表达",
                        }
                    ]
                ),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ISSUE_SENTENCE,
                            "replace": IMPROVED_SENTENCE,
                            "rationale": "精简表达",
                        }
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(logger=lambda message: None)
        result = harness.start("请全面检查并修改这篇论文。", DOC)
        self.assertTrue(result.is_interrupted)

        self.assertEqual(result.pending_request["type"], "patch_review")
        result = harness.resume({"reject_all": True})

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state["applied_patches"], [])
        self.assertEqual(result.state["revision_edits"], [])
        self.assertEqual(result.revised_document, DOC)
        # 被拒绝的改动要记下来，下一轮不再重复打扰用户
        self.assertEqual(len(result.state["rejected_memory"]), 1)
        self.assertEqual(result.state["rejected_memory"][0]["span_label"], "P01")

    def test_ignoring_issues_is_remembered(self) -> None:
        """用户说“这个不用改”之后：不再进入修改，并且记进要求清单。"""

        # 忽略机制**保留**（只是不再在闸门里问用户"要忽略哪些"）。
        # 这里直接验证机制本身：给出ignore决定后，问题被标成ignored，
        # 并生成一条可跨轮识别的ignore规则（下一轮不会再问同一批问题）。
        from app.workflow.nodes import normalize_issue_payload

        findings = [
            {
                "id": 1,
                "span_label": "P01",
                "category": "学术规范",
                "problem": "参考文献格式不统一",
            },
            {
                "id": 2,
                "span_label": "P02",
                "category": "语言表达",
                "problem": "表达冗余",
            },
        ]
        update = normalize_issue_payload(
            {"ignore_ids": [1], "rules": ["全文统一把「土豆」写成「马铃薯」"]},
            findings,
            rules=[],
            turn=1,
        )

        marked = {issue["id"]: issue.get("ignored") for issue in update["audit_findings"]}
        self.assertTrue(marked[1])  # 被点名的忽略
        self.assertFalse(marked.get(2))  # 没点名的不动

        kinds = {rule["kind"] for rule in update["user_rules"]}
        self.assertEqual(kinds, {"ignore", "rule"})
        texts = " ".join(rule["text"] for rule in update["user_rules"])
        self.assertIn("马铃薯", texts)
        self.assertIn("不改 P01", texts)


class TestDecisionGate(WorkflowTestCase):
    """
    **只有"前后矛盾、必须作者拍板"的问题**才停下来问用户。

    这里盯的是一个真实踩过的坑：`audit`节点误把`decisions`标成True，
    导致路由以为"已经问过"，决策闸门被整个跳过——用户根本没被问，
    Revision就自己替用户做了决定（删限定语、改数值口径）。
    """

    def _install(self, kind: str):
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "kind": kind,
                            "category": "实验分析",
                            "severity": "high",
                            "problem": "温度系数在正文与参数表中不一致",
                            "options": ["全文温度系数以 0.07 为准", "全文温度系数以 0.09 为准"],
                            "evidence": ISSUE_SENTENCE,
                            "suggestion": "请作者确认以哪一处为准",
                        }
                    ]
                ),
                REVISION_HEADER: patch_response([]),
                REVIEW_HEADER: review_response("pass"),
            }
        )

    def test_decision_issue_pauses_and_answer_reaches_revision(self) -> None:
        self._install("decision")
        harness = Harness(logger=lambda message: None)
        result = harness.start("请全面检查并修改这篇论文。", DOC)

        self.assertTrue(result.is_interrupted, "决策闸门没有停下来")
        self.assertEqual(result.pending_request["type"], "issue_review")
        self.assertEqual([issue["id"] for issue in result.pending_request["issues"]], [1])

        # 用户的判断会被记成规则，Revision拿得到
        result = harness.resume({"rules": ["温度系数以 0.07 为准"]})
        self.assertTrue(result.state["decisions"])
        self.assertIn("0.07", result.state["user_rules"][0]["text"])

    def test_plain_fix_goes_straight_to_revision(self) -> None:
        """写错的问题（fix）不该打扰用户，直接进Revision。"""

        self._install("fix")
        harness = Harness(logger=lambda message: None)
        result = harness.start("请全面检查并修改这篇论文。", DOC)

        self.assertTrue(result.is_interrupted)
        self.assertEqual(result.pending_request["type"], "patch_review")
        # audit节点绝不能替用户把"已经问过"标上
        self.assertFalse(result.state.get("decisions"))


class TestReplanningImpl(WorkflowTestCase):
    def test_review_fail_triggers_replanning_until_cap(self) -> None:
        calls = {"count": 0}

        def orchestrator(prompt: str) -> str:
            # 首轮规划走Audit；Review FAIL后的重规划直接回到Revision
            return plan_response("revision" if "重新规划" in prompt else "audit")

        def revision(prompt: str) -> str:
            # 每轮改一个不同的地方，模拟“改了但Review仍不通过”
            calls["count"] += 1
            if calls["count"] == 1:
                return patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ISSUE_SENTENCE,
                            "replace": IMPROVED_SENTENCE,
                            "rationale": "补充分析",
                        }
                    ]
                )
            return patch_response(
                [
                    {
                        "span_label": "P02",
                        "issue_id": 1,
                        "quote": INTRO_SENTENCE,
                        "replace": INTRO_IMPROVED,
                        "rationale": "补充小样本场景的说明",
                    }
                ]
            )

        self.install(
            {
                ORCHESTRATOR_HEADER: orchestrator,
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": "P01",
                            "category": "实验分析",
                            "severity": "high",
                            "problem": "消融实验分析不足",
                            "evidence": ISSUE_SENTENCE,
                            "suggestion": "补充消融实验的分析与结论",
                        }
                    ]
                ),
                REVISION_HEADER: revision,
                REVIEW_HEADER: review_response(
                    "fail",
                    remaining_issues=[
                        {
                            "id": 1,
                            "category": "实验分析",
                            "severity": "high",
                            "span_label": "P02",
                            "problem": "消融实验分析仍然不足",
                            "suggestion": "继续补充分析",
                        }
                    ],
                ),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC, max_iterations=2)

        self.assertEqual(result.status, "completed")
        # 达到迭代上限后熔断，输出当前最优结果
        self.assertEqual(result.state["iteration"], 2)
        self.assertEqual(result.review_status, "fail")
        self.assertIn("迭代上限", result.final_report)

        # Orchestrator至少被调用两次（初始规划 + 重规划）
        plan_events = [
            event
            for event in result.state["trace"]
            if event.get("agent") == "workflow" and event.get("action") == "orchestrator"
        ]
        self.assertGreaterEqual(len(plan_events), 2)


class TestDegradedPaths(WorkflowTestCase):
    def test_audit_failure_is_isolated(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: "这不是 JSON，模型完全跑偏了",
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.state["audit_failed"])
        self.assertEqual(result.state["audit_findings"], [])
        self.assertTrue(result.report_path)

    def test_no_issues_found_goes_to_report(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("audit"),
                AUDIT_HEADER: audit_response([]),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        self.assertEqual(result.status, "completed")
        self.assertFalse(result.is_interrupted)
        self.assertEqual(result.state["audit_findings"], [])
        self.assertIn("未发现问题", result.final_report)

    def test_orchestrator_llm_failure_uses_fallback(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: "抱歉，我无法输出 JSON。",
                AUDIT_HEADER: audit_response([]),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("请全面检查并修改这篇论文。", DOC)

        self.assertEqual(result.status, "completed")
        # 规则兜底：用户要求全面检查 -> 先Audit
        self.assertEqual(result.state["plan_kind"], "full_review")
        self.assertEqual(result.state["audit_meta"].get("chunks"), 1)


class TestDirectRevision(WorkflowTestCase):
    def test_direct_revision_skips_audit(self) -> None:
        self.install(
            {
                ORCHESTRATOR_HEADER: plan_response("revision"),
                REVISION_HEADER: patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": ISSUE_SENTENCE,
                            "replace": IMPROVED_SENTENCE,
                            "rationale": "按用户要求直接修改",
                        }
                    ]
                ),
                REVIEW_HEADER: review_response("pass"),
            }
        )

        harness = Harness(auto_approve=True, logger=lambda message: None)
        result = harness.run_to_completion("只修改摘要，不要审查全文。", DOC)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state["audit_findings"], [])
        self.assertIn(IMPROVED_SENTENCE, result.revised_document)
        self.assertEqual(result.review_status, "pass")


if __name__ == "__main__":
    unittest.main()
