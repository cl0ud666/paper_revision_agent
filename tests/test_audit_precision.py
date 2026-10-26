"""
Audit 精度测试：好论文不该被报一堆问题。

覆盖：

1. 证据必须能在段落里逐字找到，否则丢弃（防幻觉）；
2. 默认丢掉 low（可改可不改）；
3. 同一类系统性问题合并成一条（"参考文献格式不统一（共 3 处）"）；
4. 封面/目录/致谢不进审查；
5. 分批时后面的批次能看到前面批次已提出的改动（跨批次对齐）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents import audit as audit_agent
from app.agents import revision as revision_agent
from app.agents.prompts import AUDIT_HEADER, NAME_CHECK_HEADER, REVISION_HEADER
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import reset_checkpointers
from tests.fakes import (
    audit_response,
    fake_factory,
    name_check_response,
    patch_response,
)

DOC = """# 论文标题

## 摘要

本文提出了一个新的方法，效果比较好。实验结果表明我们的方法比基线方法好。

## 1 引言

图神经网络在节点分类任务上被广泛使用。

## 2 相关工作

现有方法主要分为两类。第一类基于卷积，第二类基于注意力。
"""

ABSTRACT = "本文提出了一个新的方法，效果比较好。"
INTRO = "图神经网络在节点分类任务上被广泛使用。"


class AuditPrecisionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-audit-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()

    def tearDown(self) -> None:
        reset_llm_factory()
        for key in ("PRA_DATA_DIR", "PRA_RUNS_DIR", "PRA_RETRY_MAX_ATTEMPTS", "PRA_AUDIT_MIN_SEVERITY"):
            os.environ.pop(key, None)
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_audit(self, issues: list[dict], **state_overrides) -> dict:
        set_llm_factory(fake_factory({AUDIT_HEADER: audit_response(issues)}))
        state = {
            "run_id": "audit-test",
            "current_document": DOC,
            "document_version": 1,
            "user_request": "全面检查",
            **state_overrides,
        }
        return audit_agent.run(state, logger=lambda message: None)


class TestEvidenceVerification(AuditPrecisionTestCase):
    def test_unverifiable_evidence_is_dropped(self) -> None:
        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "语言表达",
                    "severity": "high",
                    "problem": "编造的问题（原文里没有这个片段）",
                    "evidence": "这句话在原文里根本不存在，是模型编出来的",
                }
            ]
        )
        self.assertEqual(update["audit_findings"], [])
        self.assertEqual(update["audit_meta"]["dropped_unverifiable"], 1)

    def test_verified_issue_is_kept(self) -> None:
        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "语言表达",
                    "severity": "high",
                    "problem": "结论表述过于笼统",
                    "evidence": ABSTRACT,
                }
            ]
        )
        self.assertEqual(len(update["audit_findings"]), 1)
        self.assertEqual(update["audit_meta"]["dropped_unverifiable"], 0)


class TestNameCheck(AuditPrecisionTestCase):
    """
    专名核验：**正确写法不需要在文中出现过**。

    "阿里云 ESC"应为 ECS——这是世界知识，不是前后文比对：
    全篇只写了一次错字、正确写法一次都没出现，也必须能报出来。
    """

    TYPO_DOC = (
        "# 论文标题\n\n"
        "## 摘要\n\n"
        "本文提出的方法在大规模图上进行了验证，效果良好，值得推广。\n\n"
        "## 3 实验\n\n"
        "全部实验在阿里云 ESC 上完成，使用 4 张显卡训练了三天。\n"
    )

    def run_audit_with(self, suspected, **overrides) -> tuple[dict, list]:
        calls: list[dict] = []
        set_llm_factory(
            fake_factory(
                {
                    AUDIT_HEADER: audit_response([]),
                    NAME_CHECK_HEADER: name_check_response(suspected),
                },
                calls=calls,
            )
        )
        state = {
            "run_id": "name-check-test",
            "current_document": overrides.pop("document", self.TYPO_DOC),
            "document_version": 1,
            "user_request": "全面检查",
            **overrides,
        }
        return audit_agent.run(state, logger=lambda message: None), calls

    def test_typo_without_any_correct_form_is_reported(self) -> None:
        """全篇只有一处 ESC、没有 ECS——靠世界知识也要报出来。"""

        update, calls = self.run_audit_with(
            [
                {
                    "name": "ESC",
                    "correct": "ECS",
                    "reason": "阿里云的弹性计算服务官方名称是 ECS",
                    "confidence": "high",
                    "span_label": "P02",
                    "evidence": "全部实验在阿里云 ESC 上完成，使用 4 张显卡训练了三天。",
                    "context_basis": "全部实验在阿里云 ESC 上完成",
                }
            ],
            document=self.TYPO_DOC,  # 文中没有 ECS
        )

        # 确实发起了专名核验这一次调用
        self.assertIn(NAME_CHECK_HEADER, [call["header"] for call in calls])
        findings = update["audit_findings"]
        self.assertEqual(len(findings), 1)
        issue = findings[0]
        self.assertEqual(issue["category"], "专有名词")
        self.assertIn("ECS", issue["problem"])
        self.assertIn("阿里云 ESC", issue["evidence"])  # 证据逐字来自原文
        self.assertEqual(issue["source"], "name_check")
        self.assertEqual(update["audit_meta"]["name_check_found"], 1)

    def test_invented_name_is_rejected(self) -> None:
        """模型报一个清单里根本没出现的词 → 丢弃（防幻觉）。"""

        update, _ = self.run_audit_with(
            [
                {
                    "name": "FROB",
                    "correct": "PROB",
                    "reason": "编造的词",
                    "confidence": "high",
                }
            ]
        )
        self.assertEqual(update["audit_findings"], [])

    def test_low_confidence_is_dropped(self) -> None:
        update, _ = self.run_audit_with(
            [
                {
                    "name": "ESC",
                    "correct": "ECS",
                    "reason": "好像不太对",
                    "confidence": "low",
                }
            ]
        )
        self.assertEqual(update["audit_findings"], [])

    def test_document_without_names_skips_the_extra_call(self) -> None:
        plain = "## 摘要\n\n本文提出了一个新的方法，效果比较好。\n"
        update, calls = self.run_audit_with([], document=plain)
        self.assertNotIn(NAME_CHECK_HEADER, [call["header"] for call in calls])
        self.assertEqual(update["audit_findings"], [])

    def test_similar_terms_alone_do_not_create_an_error(self) -> None:
        """即使存在形近对照，也必须由上下文核验支持，不能靠多数写法判错。"""

        document = (
            "## 3 实验\n\n"
            "全部实验在阿里云 ECS 上完成，使用 4 张显卡训练了三天。\n\n"
            "推理同样在阿里云 ECS 上完成，批量大小为 32。\n\n"
            "## 4 讨论\n\n"
            "部署环境为阿里云 ESC，成本较低。\n"
        )
        update, _ = self.run_audit_with([], document=document)
        findings = update["audit_findings"]
        self.assertEqual(findings, [])

    def test_gnn_knn_cnn_rnn_are_not_frequency_errors_in_audit_or_review(self):
        from app.agents.review import _precheck
        document = ("图神经网络 GNN 通过邻居节点的消息传递学习图表示。\n\n"
                    "本实验的分类基线为 KNN，KNN 按近邻投票分类，另与 KNN 的结果比较。\n\n"
                    "CNN 通过卷积提取局部特征，CNN 可以提取序列特征。\n\n"
                    "RNN 是循环神经网络，本实验也比较了 RNN 的性能。\n")
        update, _ = self.run_audit_with([], document=document)
        self.assertEqual(update["audit_findings"], [])
        _, remaining, _ = _precheck({}, document=document, original=document, targets=[])
        self.assertFalse(any("GNN" in i.get("problem", "") for i in remaining))

    def test_frequency_reason_is_rejected_even_with_high_confidence(self):
        document = "图神经网络 GNN 通过消息传递学习节点表示。\n\nKNN 按近邻投票分类，KNN 是基线。\n"
        update, _ = self.run_audit_with([{
            "name": "GNN", "correct": "KNN", "span_label": "P01", "confidence": "high",
            "evidence": "图神经网络 GNN 通过消息传递学习节点表示。",
            "context_basis": "通过消息传递学习节点表示", "reason": "文中多处写作 KNN，只差一个字符，疑似笔误",
        }], document=document)
        self.assertEqual(update["audit_findings"], [])

    def test_contextual_error_only_targets_the_wrong_occurrence(self):
        document = "图神经网络 GNN 用消息传递学习节点表示。\n\n本文将 k 近邻分类器记为 GNN，按照最近邻的类别投票。\n"
        update, _ = self.run_audit_with([{
            "name": "GNN", "correct": "KNN", "span_label": "P02", "confidence": "high",
            "evidence": "本文将 k 近邻分类器记为 GNN，按照最近邻的类别投票。",
            "context_basis": "按照最近邻的类别投票", "reason": "本段明确定义为 k 近邻分类器，对应 KNN；GNN 指图神经网络，与此定义矛盾",
        }], document=document)
        findings = update["audit_findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["span_label"], "P02")
        self.assertIn("仅在 [P02]", findings[0]["suggestion"])

    def test_missing_or_invented_context_evidence_is_rejected(self):
        for evidence in ("", "阿里云 ESC 不存在于本段的句子"):
            with self.subTest(evidence=evidence):
                update, _ = self.run_audit_with([{
                    "name": "ESC", "correct": "ECS", "confidence": "high", "span_label": "P02",
                    "evidence": evidence, "context_basis": "阿里云 ESC", "reason": "该段使用阿里云弹性计算服务",
                }])
                self.assertEqual(update["audit_findings"], [])

    def test_flag_can_turn_the_check_off(self) -> None:
        os.environ["PRA_AUDIT_NAME_CHECK"] = "0"
        reset_settings()
        try:
            update, calls = self.run_audit_with(
                [],
                document=(
                    "## 3 实验\n\n全部实验在阿里云 ECS 上完成，使用 4 张显卡。\n\n"
                    "推理同样在阿里云 ECS 上完成，批量大小为 32。\n\n"
                    "## 4 讨论\n\n部署环境为阿里云 ESC，成本较低。\n"
                ),
            )
        finally:
            os.environ.pop("PRA_AUDIT_NAME_CHECK", None)
            reset_settings()
        self.assertNotIn(NAME_CHECK_HEADER, [call["header"] for call in calls])
        self.assertEqual(update["audit_findings"], [])


class TestSeverityThreshold(AuditPrecisionTestCase):
    def test_low_severity_dropped_by_default(self) -> None:
        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "语言表达",
                    "severity": "low",
                    "problem": "措辞可以更漂亮一点",
                    "evidence": ABSTRACT,
                }
            ]
        )
        self.assertEqual(update["audit_findings"], [])
        self.assertEqual(update["audit_meta"]["dropped_low_severity"], 1)

    def test_low_severity_kept_when_configured(self) -> None:
        os.environ["PRA_AUDIT_MIN_SEVERITY"] = "low"
        reset_settings()
        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "语言表达",
                    "severity": "low",
                    "problem": "措辞可以更漂亮一点",
                    "evidence": ABSTRACT,
                }
            ]
        )
        self.assertEqual(len(update["audit_findings"]), 1)


class TestSystematicMerging(AuditPrecisionTestCase):
    def test_same_kind_issue_across_spans_becomes_one(self) -> None:
        """同一个毛病在 3 个段落出现 → 只报一条，并列出涉及的段落。"""

        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "学术规范",
                    "severity": "medium",
                    "problem": "参考文献格式不统一，缺少出版信息",
                    "evidence": ABSTRACT,
                },
                {
                    "span_label": "P02",
                    "category": "学术规范",
                    "severity": "medium",
                    "problem": "参考文献格式不统一，卷号与页码的写法前后不一致",
                    "evidence": INTRO,
                },
                {
                    "span_label": "P03",
                    "category": "学术规范",
                    "severity": "medium",
                    "problem": "参考文献格式不统一，作者姓名的写法不一致",
                    "evidence": "现有方法主要分为两类。第一类基于卷积，第二类基于注意力。",
                },
            ]
        )
        findings = update["audit_findings"]

        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0]["affected_spans"]), 3)
        self.assertIn("同类问题共 3 处", findings[0]["problem"])
        self.assertEqual(update["audit_meta"]["merged_systematic"], 2)

    def test_different_categories_are_not_merged(self) -> None:
        update = self.run_audit(
            [
                {
                    "span_label": "P01",
                    "category": "学术规范",
                    "severity": "medium",
                    "problem": "参考文献格式不统一",
                    "evidence": ABSTRACT,
                },
                {
                    "span_label": "P02",
                    "category": "实验分析",
                    "severity": "high",
                    "problem": "实验结果缺乏对比分析",
                    "evidence": INTRO,
                },
            ]
        )
        self.assertEqual(len(update["audit_findings"]), 2)


class TestAuditScope(AuditPrecisionTestCase):
    def test_cover_and_toc_not_sent_to_model(self) -> None:
        prompts: list[str] = []

        def audit(prompt: str) -> str:
            prompts.append(prompt)
            return audit_response([])

        set_llm_factory(fake_factory({AUDIT_HEADER: audit}))
        document = (
            "本 科 毕 业 设 计（论 文）\n\n题    目\n\n某论文题目\n\n"
            "摘    要\n\n" + ABSTRACT + "\n\n"
            "目    录\n\n1 引言 ........................ 1\n\n"
            "1 引言\n\n" + INTRO + "\n"
        )
        state = {
            "run_id": "scope-test",
            "current_document": document,
            "document_version": 1,
            "user_request": "全面检查",
        }
        update = audit_agent.run(state, logger=lambda message: None)

        self.assertTrue(prompts)
        joined = "\n".join(prompts)
        self.assertNotIn("某论文题目", joined)   # 封面
        self.assertNotIn("........................", joined)  # 目录条目
        self.assertIn(ABSTRACT, joined)          # 摘要要审
        self.assertIn(INTRO, joined)             # 正文要审
        self.assertEqual(update["audit_meta"]["scope"], "body")
        self.assertGreater(update["audit_meta"]["skipped_noise"], 0)


class TestCrossBatchVisibility(AuditPrecisionTestCase):
    def test_later_batch_sees_earlier_patches(self) -> None:
        """分批提补丁时，后面的批次要能看到前面批次已提出的改动。"""

        prompts: list[str] = []

        def revision(prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return patch_response(
                    [
                        {
                            "span_label": "P01",
                            "issue_id": 1,
                            "quote": first_quote,
                            "replace": "本文提出一种面向小样本场景的方法。",
                            "rationale": "把结论改具体",
                        }
                    ]
                )
            return patch_response([])

        # 造一份足够长的文档：预算压到很小时才会分成多批
        paragraphs = [
            f"## {index} 第{index}节\n\n这是第 {index} 节的正文内容，用于把文档撑长以便分批处理。" * 3
            for index in range(1, 13)
        ]
        long_doc = "# 论文标题\n\n" + "\n\n".join(paragraphs) + "\n"
        first_quote = "这是第 1 节的正文内容，用于把文档撑长以便分批处理。"
        second_quote = "这是第 12 节的正文内容，用于把文档撑长以便分批处理。"

        set_llm_factory(fake_factory({REVISION_HEADER: revision}))
        issues = [
            {
                "id": 1,
                "category": "语言表达",
                "severity": "high",
                "problem": "结论笼统",
                "evidence": first_quote,
            },
            {
                "id": 2,
                "category": "术语使用",
                "severity": "medium",
                "problem": "术语不统一",
                "evidence": second_quote,
            },
        ]
        state = {
            "run_id": "cross-batch",
            "current_document": long_doc,
            "document_version": 1,
            "user_request": "全面检查",
            "audit_findings": issues,
            "issue_gate": True,
            "next_instructions": "",
        }

        os.environ["PRA_REVISION_MAX_TOKENS"] = "600"
        reset_settings()
        try:
            update = revision_agent.run(state, logger=lambda message: None)
        finally:
            os.environ.pop("PRA_REVISION_MAX_TOKENS", None)
            reset_settings()

        self.assertGreaterEqual(len(prompts), 2, "预算很小时应当分成多批")
        if len(prompts) >= 2:
            # 第二个批次的提示里要能看到第一个批次提出的改动
            self.assertIn("本轮其它批次已提出的改动", prompts[1])
            self.assertIn("面向小样本场景", prompts[1])
        self.assertTrue(update["proposed_patches"] is not None)


if __name__ == "__main__":
    unittest.main()
