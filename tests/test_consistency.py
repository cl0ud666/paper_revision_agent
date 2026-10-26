"""
跨批次一致性与删除类修改测试。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents import review as review_agent
from app.agents.prompts import REVIEW_HEADER
from app.config import reset_settings
from app.document.patches import (
    STATUS_APPLIED,
    apply_patches,
    patch_from_model,
    validate_patch,
)
from app.document.spans import build_spans
from app.utils.consistency import (
    describe_entity_check,
    describe_naming_variants,
    find_cross_section_gaps,
    find_naming_variants,
    find_near_miss_names,
    find_structure_damage,
    find_inconsistencies,
    term_mappings_from_patches,
    term_mappings_from_rules,
)
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from tests.fakes import fake_factory, review_response

DOC = """# 论文标题

## 摘要

本文针对土豆的检测问题提出方法。

## 1 引言

土豆是重要的农作物，土豆的检测一直是个难题。

## 2 方法

我们使用马铃薯作为主要研究对象，并提出新的检测流程。
"""


class TestTermMappings(unittest.TestCase):
    def test_mappings_from_user_rules(self) -> None:
        rules = [
            {"id": "R1", "kind": "rule", "text": "全文统一把「土豆」写成「马铃薯」", "status": "active"},
            {"id": "R2", "kind": "ignore", "text": "不改 P05", "status": "active"},
        ]
        mappings = term_mappings_from_rules(rules)
        self.assertEqual(mappings[0]["old"], "土豆")
        self.assertEqual(mappings[0]["new"], "马铃薯")
        self.assertEqual(len(mappings), 1)

    def test_mappings_from_patches_only_for_short_edits(self) -> None:
        patches = [
            {"patch_id": "patch-001", "before": "土豆", "replace": "马铃薯", "span_label": "P02"},
            {
                "patch_id": "patch-002",
                "before": "这是一整段被重写的长句子，不应该被当成术语替换。",
                "replace": "换成了另一段同样很长的内容。",
                "span_label": "P09",
            },
        ]
        mappings = term_mappings_from_patches(patches)
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]["old"], "土豆")

    def test_find_inconsistencies(self) -> None:
        spans = build_spans(DOC)
        items = find_inconsistencies(
            DOC,
            mappings=[{"old": "土豆", "new": "马铃薯", "source": "patch-001"}],
            spans=spans,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 3)
        self.assertEqual(len(items[0]["spans"]), 2)   # 摘要 + 引言（方法那段已经用马铃薯）

    def test_fixed_spans_are_excluded(self) -> None:
        spans = build_spans(DOC)
        abstract = next(span for span in spans if span.text.startswith("本文针对"))
        items = find_inconsistencies(
            DOC,
            mappings=[{"old": "土豆", "new": "马铃薯"}],
            spans=spans,
            fixed_spans=[abstract.label],
        )
        self.assertNotIn(abstract.label, items[0]["spans"])


class TestReviewConsistencyCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-consistency-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        reset_settings()
        reset_guards()
        reset_recorders()

    def tearDown(self) -> None:
        reset_llm_factory()
        for key in ("PRA_DATA_DIR", "PRA_RUNS_DIR"):
            os.environ.pop(key, None)
        reset_settings()
        reset_guards()
        reset_recorders()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_review_flags_remaining_old_term(self) -> None:
        """批 2 已改成"马铃薯"，但别处还写着"土豆" → Review 必须指出来。"""

        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        state = {
            "run_id": "consistency-review",
            "user_request": "全面检查",
            "original_document": DOC,
            "current_document": DOC,
            "audit_findings": [
                {
                    "id": 1,
                    "span_label": "P03",
                    "category": "术语使用",
                    "severity": "medium",
                    "problem": "术语不统一",
                    "evidence": "我使用马铃薯作为主要研究对象",
                }
            ],
            "applied_patches": [
                {
                    "patch_id": "patch-001",
                    "issue_id": 1,
                    "span_label": "P03",
                    "before": "土豆",
                    "replace": "马铃薯",
                    "rationale": "统一术语",
                    "status": "applied",
                }
            ],
            "proposed_patches": [],
            "rejected_patches": [],
            "iteration": 1,
            "max_iterations": 3,
        }
        update = review_agent.run(state, logger=lambda message: None)
        result = update["review_result"]

        self.assertEqual(result["status"], "fail")
        checks = {check["name"]: check["status"] for check in result["checks"]}
        self.assertEqual(checks["跨批次一致性"], "warn")
        problems = " ".join(issue.get("problem", "") for issue in result["remaining_issues"])
        self.assertIn("马铃薯", problems)
        self.assertIn("土豆", problems)

    def test_user_rule_drives_consistency(self) -> None:
        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        state = {
            "run_id": "consistency-rule",
            "user_request": "全面检查",
            "original_document": DOC,
            "current_document": DOC,
            "audit_findings": [],
            "applied_patches": [],
            "proposed_patches": [],
            "rejected_patches": [],
            "user_rules": [
                {
                    "id": "R1",
                    "kind": "rule",
                    "text": "全文统一把「土豆」写成「马铃薯」",
                    "status": "active",
                }
            ],
            "iteration": 1,
            "max_iterations": 3,
        }
        update = review_agent.run(state, logger=lambda state: None)
        problems = " ".join(
            issue.get("problem", "") for issue in update["review_result"]["remaining_issues"]
        )
        self.assertIn("土豆", problems)


class TestDeletePatch(unittest.TestCase):
    def setUp(self) -> None:
        self.document = "第一句保留。第二句是冗余重复的，应该删掉。第三句保留。\n"
        self.spans = build_spans(self.document)
        self.span = self.spans[0]

    def test_delete_patch_removes_text(self) -> None:
        patch = patch_from_model(
            {
                "span_label": self.span.label,
                "quote": "第二句是冗余重复的，应该删掉。",
                "replace": "",
                "delete": True,
                "rationale": "删除与第一句重复的表述",
            },
            spans=self.spans,
            patch_id="patch-001",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.status, "proposed")
        self.assertIn("delete", patch.strategy)

        patch.status = "accepted"
        outcome = apply_patches(self.document, self.spans, [patch])
        self.assertEqual(outcome.applied[0].status, STATUS_APPLIED)
        self.assertNotIn("应该删掉", outcome.document)
        self.assertIn("第一句保留", outcome.document)

    def test_empty_replace_without_flag_is_conflict(self) -> None:
        patch = patch_from_model(
            {
                "span_label": self.span.label,
                "quote": "第二句是冗余重复的，应该删掉。",
                "replace": "",
                "rationale": "删除冗余",
            },
            spans=self.spans,
            patch_id="patch-002",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.status, "conflict")
        self.assertIn("已保留原文", patch.conflict_reason)
        self.assertFalse(patch.delete)


class TestProperNounVariants(unittest.TestCase):
    """
    专有名词写法检查（确定性，不调用模型）。

    模型一次只看一段，而"同一个词写了两种样子"是**全文属性**；
    这里先把证据摆出来，模型再据此写成一条有据可依的问题。
    """

    def spans(self, *paragraphs):
        return build_spans("\n\n".join(paragraphs) + "\n")

    def test_same_name_written_two_ways_is_detected(self):
        spans = self.spans("我们在 PubMed 上做了实验。", "同时在 Pubmed 上也做了对比。")
        items = find_naming_variants(spans)

        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(items[0]["variants"]), ["PubMed", "Pubmed"])
        self.assertEqual(items[0]["variants"]["PubMed"], ["P01"])
        self.assertEqual(items[0]["variants"]["Pubmed"], ["P02"])

    def test_hyphen_variant_is_detected(self):
        spans = self.spans("GraphSAGE 的效果很好。", "Graph-SAGE 也被比较过。")
        items = find_naming_variants(spans)
        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(items[0]["variants"]), ["Graph-SAGE", "GraphSAGE"])

    def test_sentence_initial_capital_is_not_reported(self):
        """普通英文词的句首大写不算"写法不一致"，否则满篇误报。"""

        spans = self.spans("We propose a method.", "we also compare it with baselines.")
        self.assertEqual(find_naming_variants(spans), [])

    def test_single_writing_is_not_reported(self):
        spans = self.spans("PubMed 是我们用的数据集。")
        self.assertEqual(find_naming_variants(spans), [])

    def test_description_names_every_form_and_paragraph(self):
        spans = self.spans("我们在 PubMed 上做了实验。", "同时在 Pubmed 上也做了对比。")
        text = describe_naming_variants(find_naming_variants(spans))
        self.assertIn("PubMed", text)
        self.assertIn("Pubmed", text)
        self.assertIn("P01", text)
        self.assertIn("P02", text)

    def test_audit_context_carries_the_hint(self):
        from app.utils.context import build_context

        fields = build_context(
            {},
            "audit",
            extra={"consistency_check": "- 同一个词写作：「PubMed」（P01） / 「Pubmed」（P02）"},
        )
        self.assertIn("consistency_check", fields)
        self.assertIn("确定性扫描", fields["consistency_check"])
        self.assertIn("PubMed", fields["consistency_check"])

    def test_audit_context_omits_the_hint_when_nothing_found(self):
        from app.utils.context import build_context

        fields = build_context({}, "audit", extra={"consistency_check": ""})
        self.assertNotIn("consistency_check", fields)

    def test_audit_prompt_asks_for_proper_nouns(self):
        from app.agents.prompts import render_audit_prompt

        prompt = render_audit_prompt("# 上下文")
        self.assertIn("专有名词", prompt)
        self.assertIn("一致性核对提示", prompt)
        self.assertIn("PubMed", prompt)
        # 段落开头的排版标记不许被当成正文引用
        self.assertIn("排版标记", prompt)

    def test_category_is_registered(self):
        """新类目必须在枚举里，否则会被静默改写成"语言表达"。"""

        from app.state.schema import ISSUE_CATEGORIES

        self.assertIn("专有名词", ISSUE_CATEGORIES)
        from app.agents.audit import _normalize_issue

        spans = self.spans("我们在 Pubmed 上做了实验，效果不错。")
        issue = _normalize_issue(
            {
                "span_label": spans[0].label,
                "category": "专有名词",
                "severity": "medium",
                "problem": "同一个数据集在别处写作 PubMed，写法不统一",
                "evidence": "我们在 Pubmed 上做了实验，效果不错",
            },
            chunk=spans,
            document=spans[0].text,
        )
        self.assertEqual(issue["category"], "专有名词")


class TestCrossSectionChecks(unittest.TestCase):
    """跨章节一致性：只在摘要/结论出现、正文没有的专有名词（BERT 那一类）。"""

    def test_summary_only_name_is_reported(self):
        document = (
            "## 摘要\n\n"
            "本文使用 BERT 与 GCN 完成了节点分类。\n\n"
            "## 3 实验\n\n"
            "我们在 Cora 上训练了 GCN，效果比基线好。\n\n"
            "## 5 结论\n\n"
            "本文提出的 BERT 方案在小样本场景下有效。\n"
        )
        gaps = find_cross_section_gaps(build_spans(document))
        names = [item["name"] for item in gaps]

        self.assertIn("BERT", names)  # 摘要和结论都提了，正文（实验）没有
        self.assertNotIn("GCN", names)  # 正文里出现过，不算问题

    def test_body_only_name_is_not_reported(self):
        """只在正文出现、总结没提：不是问题（不能反过来误报）。"""

        document = "## 摘要\n\n本文提出了一种新方法。\n\n## 3 实验\n\n我们用了 GraphSAGE 做对比。\n"
        self.assertEqual(find_cross_section_gaps(build_spans(document)), [])

    def test_common_english_words_are_not_names(self):
        """普通英文词与句首大写不算专有名词，否则会满篇误报。"""

        document = "## 摘要\n\nWe propose a method and Table 1 shows the results.\n\n## 3 实验\n\n我们做了实验。\n"
        self.assertEqual(find_cross_section_gaps(build_spans(document)), [])

    def test_near_miss_acronym_is_reported(self):
        """阿里云 ESC / ECS 这类只差一个字符的写法。"""

        document = (
            "## 3 实验\n\n"
            "我们把模型部署到了阿里云 ECS 上，使用了 4 张 V100。\n\n"
            "训练与推理都在阿里云 ECS 完成。\n\n"
            "## 4 讨论\n\n"
            "部署环境为阿里云 ESC。\n"
        )
        items = find_near_miss_names(build_spans(document))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["suspected"], "ESC")
        self.assertEqual(items[0]["likely"], "ECS")
        self.assertTrue(items[0]["likely_labels"])
        self.assertTrue(items[0]["suspected_labels"])

    def test_different_but_valid_acronyms_are_not_flagged(self):
        """GAT 与 GCN 都是真实存在的模型名，不能当成拼写错误。"""

        document = "## 2 相关工作\n\nGAT 引入了注意力，GCN 聚合邻居信息，两者都被广泛使用。\n"
        self.assertEqual(find_near_miss_names(build_spans(document)), [])

    def test_numbered_codes_are_not_spelling_errors(self):
        """H01/P01、V100/V200 这类编号与型号，差一个字符也不算拼错。"""

        document = "## 3 方法\n\n见 H01 与 H02 两处说明。\n\n对比 P01、T01 三个段落。\n"
        self.assertEqual(find_near_miss_names(build_spans(document)), [])

    def test_entity_check_description_lists_both_kinds(self):
        spans = build_spans(
            "## 摘要\n\n本文使用 BERT 完成分类。\n\n"
            "## 5 结论\n\nBERT 方案在阿里云 ESC 上表现良好。\n\n"
            "## 3 实验\n\n我们在阿里云 ECS 上训练了模型。\n"
        )
        sections = describe_entity_check(
            find_cross_section_gaps(spans), find_near_miss_names(spans)
        )
        text = "\n".join(sections)
        self.assertIn("BERT", text)
        self.assertIn("正文从未出现", text)
        self.assertIn("ESC", text)


class TestStructureDamage(unittest.TestCase):
    """排版标记被写进正文：必须在复核阶段被确定性抓住。"""

    def test_doubled_heading_marker_is_reported(self):
        spans = build_spans("### ### 3.3.5 对比损失函数优化\n\n正文一段。\n")
        items = find_structure_damage(spans)
        self.assertTrue(items)
        self.assertEqual(items[0]["label"], "H01")

    def test_interior_marker_is_reported(self):
        spans = build_spans("### 3.3.5 ### 对比损失函数优化\n")
        self.assertTrue(find_structure_damage(spans))

    def test_normal_headings_are_clean(self):
        spans = build_spans("# 标题\n\n## 3.3.5 对比损失函数优化\n\n- 要点一\n\n正文。\n")
        self.assertEqual(find_structure_damage(spans), [])


if __name__ == "__main__":
    unittest.main()
