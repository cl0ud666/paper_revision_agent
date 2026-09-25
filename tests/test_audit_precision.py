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
from app.agents.prompts import AUDIT_HEADER, REVISION_HEADER
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import reset_checkpointers
from tests.fakes import audit_response, fake_factory, patch_response

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
                            "quote": ABSTRACT,
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
