"""
文档模型测试：段落/句子稳定 ID、补丁定位与校验、逐条接受/拒绝、一次性应用。
"""

from __future__ import annotations

import unittest

from app.document.patches import (
    STATUS_APPLIED,
    STATUS_CONFLICT,
    STATUS_REJECTED,
    apply_patches,
    decide_patch,
    patch_from_model,
    strip_sentence_markers,
    summarize_patches,
    validate_patch,
)
from app.document.spans import (
    build_spans,
    render_annotated,
    render_outline,
    resolve_span,
    split_sentences,
)

DOC = """# 论文标题

## 摘要

本文提出了一种新方法，效果比较好。实验结果表明我们的方法更好。

## 1 引言

图神经网络被广泛使用。但是小样本场景下的效果并不理想。

## 2 方法

| 模块 | 作用 |
| --- | --- |
| 编码器 | 映射特征 |
"""


class TestSpanIds(unittest.TestCase):
    def test_build_and_labels(self) -> None:
        spans = build_spans(DOC)
        labels = [span.label for span in spans]
        self.assertIn("H01", labels)  # 论文标题
        self.assertIn("P01", labels)  # 摘要段落
        self.assertIn("T01", labels)  # 表格
        self.assertEqual(spans[-1].kind, "table")

    def test_ids_are_stable_and_idempotent(self) -> None:
        first = build_spans(DOC)
        second = build_spans(DOC)
        self.assertEqual([s.id for s in first], [s.id for s in second])

    def test_id_of_untouched_span_survives_other_edits(self) -> None:
        spans = build_spans(DOC)
        target = spans[1]  # 摘要段落

        edited = DOC.replace("图神经网络被广泛使用。", "图神经网络（GNN）已被广泛采用。")
        spans_after = build_spans(edited, version=2)
        same = resolve_span(spans_after, span_id=target.id)

        self.assertIsNotNone(same, "改别的段落后，本段落 ID 应当保持不变")
        self.assertEqual(same.text, target.text)

    def test_edited_span_gets_new_id(self) -> None:
        spans = build_spans(DOC)
        target = spans[1]
        edited = DOC.replace(target.text, target.text + "补充一句说明。")
        spans_after = build_spans(edited, version=2)
        self.assertIsNone(resolve_span(spans_after, span_id=target.id))

    def test_duplicate_paragraphs_get_distinct_ids(self) -> None:
        text = "同样的段落内容，用于测试重复情况。\n\n同样的段落内容，用于测试重复情况。\n"
        spans = build_spans(text)
        self.assertEqual(len(spans), 2)
        self.assertNotEqual(spans[0].id, spans[1].id)
        self.assertTrue(spans[1].id.endswith("-2"))

    def test_sections_are_tracked(self) -> None:
        spans = build_spans(DOC)
        abstract = next(span for span in spans if span.text.startswith("本文提出"))
        self.assertEqual(abstract.section, "## 摘要")

    def test_resolve_by_label(self) -> None:
        spans = build_spans(DOC)
        self.assertIsNotNone(resolve_span(spans, label="P01"))
        self.assertIsNotNone(resolve_span(spans, label="p1"))  # 容错：小写、无补零
        self.assertIsNone(resolve_span(spans, label="P99"))

    def test_split_sentences(self) -> None:
        sentences = split_sentences("第一句。第二句！Third sentence. Fourth one.")
        self.assertGreaterEqual(len(sentences), 3)
        self.assertEqual(sentences[0], "第一句。")

    def test_render_annotated_with_sentence_numbers(self) -> None:
        spans = build_spans(DOC)
        abstract = next(span for span in spans if span.text.startswith("本文提出"))
        text = render_annotated(spans, with_sentences_for=[abstract.id])
        self.assertIn("[P01]", text)
        self.assertIn("(1)", text)  # 被点名的段落标出句子编号

    def test_render_outline(self) -> None:
        spans = build_spans(DOC)
        outline = render_outline(spans)
        self.assertIn("P01", outline)
        self.assertIn("摘要", outline)


class TestPatchValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.spans = build_spans(DOC)
        self.abstract = next(span for span in self.spans if span.text.startswith("本文提出"))
        self.issue = {
            "id": 3,
            "category": "语言表达",
            "severity": "medium",
            "suggestion": "把笼统的结论改具体",
        }

    def make(self, **item) -> object:
        patch = patch_from_model(
            {"span_label": self.abstract.label, **item},
            spans=self.spans,
            patch_id="patch-001",
            issue=self.issue,
        )
        return validate_patch(patch, self.spans)

    def test_valid_patch_targets_span(self) -> None:
        patch = self.make(
            quote="效果比较好",
            replace="在三个数据集上的准确率均优于基线",
            rationale="把笼统表述改成可验证的结论",
        )
        self.assertEqual(patch.status, "proposed")
        self.assertEqual(patch.issue_id, 3)
        self.assertEqual(patch.rationale, "把笼统表述改成可验证的结论")
        self.assertEqual(patch.before, "效果比较好")
        self.assertEqual(patch.sentence_index, 1)  # 落在第一句
        self.assertEqual(patch.span_label, "P01")
        self.assertTrue(patch.sentence_ref.endswith(":S1"))

    def test_quote_not_in_span_is_conflict(self) -> None:
        patch = self.make(quote="这句话根本不存在于本文中", replace="x", rationale="r")
        self.assertEqual(patch.status, STATUS_CONFLICT)
        self.assertTrue(patch.conflict_reason)
        self.assertFalse(patch.decidable)

    def test_unknown_span_is_conflict(self) -> None:
        patch = patch_from_model(
            {"span_label": "P99", "quote": "任意", "replace": "x", "rationale": "r"},
            spans=self.spans,
            patch_id="patch-002",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.status, STATUS_CONFLICT)
        self.assertIn("找不到目标段落", patch.conflict_reason)

    def test_empty_replace_is_conflict(self) -> None:
        patch = self.make(quote="效果比较好", replace="   ", rationale="r")
        self.assertEqual(patch.status, STATUS_CONFLICT)
        self.assertIn("未生成具体的替换内容", patch.conflict_reason)

    def test_duplicate_quote_inside_span_is_conflict_but_cross_span_is_fine(self) -> None:
        text = (
            "## 摘要\n\n方法有效。方法有效。\n\n"
            "## 结论\n\n方法有效。\n"
        )
        spans = build_spans(text)
        duplicate_span, unique_span = spans[1], spans[3]  # 两个段落（0/2 是标题）

        ambiguous = validate_patch(
            patch_from_model(
                {
                    "span_label": duplicate_span.label,
                    "quote": "方法有效。",
                    "replace": "方法显著有效。",
                },
                spans=spans,
                patch_id="p1",
            ),
            spans,
        )
        self.assertEqual(ambiguous.status, STATUS_CONFLICT)
        self.assertIn("多次", ambiguous.conflict_reason)

        # 同一句话在别的段落也有，但因为我们限定在目标段落内，所以不构成歧义
        unique = validate_patch(
            patch_from_model(
                {
                    "span_label": unique_span.label,
                    "quote": "方法有效。",
                    "replace": "方法显著有效。",
                },
                spans=spans,
                patch_id="p2",
            ),
            spans,
        )
        self.assertEqual(unique.status, "proposed")

    def test_missing_rationale_is_warned_not_blocked(self) -> None:
        patch = self.make(quote="效果比较好", replace="表现更好")
        self.assertEqual(patch.status, "proposed")
        self.assertIn("缺少修改理由", patch.warnings)

    def test_whole_span_replacement(self) -> None:
        patch = self.make(replace="完全重写这一段。", rationale="整体重写")
        self.assertEqual(patch.status, "proposed")
        self.assertEqual(patch.strategy, "span-whole")
        self.assertEqual(patch.before, self.abstract.text)


class TestStructureMarkers(unittest.TestCase):
    """
    排版标记（### / - / |）是文档结构，不是正文。

    模型很容易把送审文本里的 ``### 3.3.5 对比损失函数优化`` 当成正文抄进修改结果，
    结果标题变成 ``### ### 3.3.5 …``，或者在 Word 里多出真实的 "###" 字符。
    这里验证补丁进来时就被剥掉，并且**最终文本只保留一个标记**。
    """

    def setUp(self) -> None:
        self.document = (
            "# 论文标题\n\n"
            "## 3.3.5 对比损失函数优化\n\n"
            "- 要点一：先做预处理。\n\n"
            "| 方法 | 准确率 |\n| --- | --- |\n| 本文方法 | 85.2 |\n"
        )
        self.spans = build_spans(self.document)

    def label_of(self, text: str) -> str:
        return next(span.label for span in self.spans if span.text == text)

    def test_heading_marker_is_stripped_from_replacement(self):
        patch = patch_from_model(
            {
                "span_label": self.label_of("## 3.3.5 对比损失函数优化"),
                "quote": "3.3.5 对比损失函数优化",
                "replace": "### 3.3.5 对比损失函数优化",
                "rationale": "改标题",
            },
            spans=self.spans,
            patch_id="patch-001",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.replace, "3.3.5 对比损失函数优化")
        self.assertTrue(any("排版标记" in warning for warning in patch.warnings))

        decide_patch(patch, "accept")
        outcome = apply_patches(self.document, self.spans, [patch])
        self.assertIn("## 3.3.5 对比损失函数优化", outcome.document)
        self.assertNotIn("### 3.3.5", outcome.document)
        self.assertNotIn("## ###", outcome.document)

    def test_quoted_marker_is_stripped_too(self):
        patch = patch_from_model(
            {
                "span_label": self.label_of("## 3.3.5 对比损失函数优化"),
                "quote": "## 3.3.5 对比损失函数优化",
                "replace": "3.3.5 对比损失函数优化与实现",
                "rationale": "补全标题",
            },
            spans=self.spans,
            patch_id="patch-001",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.quote, "3.3.5 对比损失函数优化")

        decide_patch(patch, "accept")
        outcome = apply_patches(self.document, self.spans, [patch])
        self.assertIn("## 3.3.5 对比损失函数优化与实现", outcome.document)

    def test_list_marker_is_stripped(self):
        patch = patch_from_model(
            {
                "span_label": self.label_of("- 要点一：先做预处理。"),
                "quote": "要点一：先做预处理。",
                "replace": "- 要点一：先做数据预处理与划分。",
                "rationale": "说得具体些",
            },
            spans=self.spans,
            patch_id="patch-001",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.replace, "要点一：先做数据预处理与划分。")

        decide_patch(patch, "accept")
        outcome = apply_patches(self.document, self.spans, [patch])
        self.assertIn("- 要点一：先做数据预处理与划分。", outcome.document)
        self.assertNotIn("- - ", outcome.document)

    def test_table_pipes_are_stripped(self):
        table_label = next(span.label for span in self.spans if span.kind == "table")
        patch = patch_from_model(
            {
                "span_label": table_label,
                "quote": "85.2",
                "replace": "| 87.4 |",
                "rationale": "更正数值",
            },
            spans=self.spans,
            patch_id="patch-001",
        )
        validate_patch(patch, self.spans)
        self.assertEqual(patch.replace, "87.4")

        decide_patch(patch, "accept")
        outcome = apply_patches(self.document, self.spans, [patch])
        self.assertIn("| 本文方法 | 87.4 |", outcome.document)


class TestPatchDecisionsAndApply(unittest.TestCase):
    def setUp(self) -> None:
        self.spans = build_spans(DOC)
        self.abstract = next(span for span in self.spans if span.text.startswith("本文提出"))
        self.intro = next(span for span in self.spans if span.text.startswith("图神经网络被广泛"))

    def patch(self, span, quote, replace, pid) -> object:
        return validate_patch(
            patch_from_model(
                {
                    "span_label": span.label,
                    "quote": quote,
                    "replace": replace,
                    "rationale": "测试理由",
                },
                spans=self.spans,
                patch_id=pid,
            ),
            self.spans,
        )

    def test_accept_reject_recorded(self) -> None:
        keep = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        drop = self.patch(self.intro, "被广泛使用", "应用广泛", "patch-002")

        decide_patch(keep, "accept")
        decide_patch(drop, "reject", note="")

        self.assertEqual(keep.status, "accepted")
        self.assertEqual(keep.decided_by, "user")
        self.assertEqual(drop.status, STATUS_REJECTED)
        counts = summarize_patches([keep, drop])
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["rejected"], 1)

    def test_conflict_patch_cannot_be_accepted(self) -> None:
        bad = self.patch(self.abstract, "不存在的句子", "x", "patch-003")
        decide_patch(bad, "accept")
        self.assertEqual(bad.status, STATUS_CONFLICT)

    def test_apply_only_accepted_patches(self) -> None:
        keep = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        drop = self.patch(self.intro, "被广泛使用", "被大量采用", "patch-002")
        decide_patch(keep, "accept")
        decide_patch(drop, "reject")

        outcome = apply_patches(DOC, self.spans, [keep, drop])

        self.assertIn("表现更好", outcome.document)
        self.assertNotIn("被大量采用", outcome.document)  # 被拒的不能生效
        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(outcome.applied[0].status, STATUS_APPLIED)
        self.assertTrue(outcome.applied[0].span_id_after)

    def test_apply_multiple_patches_in_one_go(self) -> None:
        first = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        second = self.patch(self.intro, "被广泛使用", "被大量采用", "patch-002")
        decide_patch(first, "accept")
        decide_patch(second, "accept")

        outcome = apply_patches(DOC, self.spans, [first, second])

        self.assertIn("表现更好", outcome.document)
        self.assertIn("被大量采用", outcome.document)
        self.assertEqual(len(outcome.applied), 2)
        self.assertEqual(outcome.spans_after[0].version, 2)

    def test_span_id_after_traces_new_location(self) -> None:
        first = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        decide_patch(first, "accept")
        outcome = apply_patches(DOC, self.spans, [first])

        new_id = outcome.applied[0].span_id_after
        self.assertNotEqual(new_id, self.abstract.id)
        self.assertIn("表现更好", resolve_span(outcome.spans_after, span_id=new_id).text)

    def test_overlapping_patches_are_flagged(self) -> None:
        first = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        second = self.patch(self.abstract, "效果比较好。实验结果表明我们的方法更好。", "整体更好。", "patch-002")
        decide_patch(first, "accept")
        decide_patch(second, "accept")

        outcome = apply_patches(DOC, self.spans, [first, second])

        self.assertEqual(len(outcome.applied), 1)
        self.assertEqual(len(outcome.skipped), 1)
        self.assertIn("重叠", outcome.skipped[0].conflict_reason)

    def test_no_accepted_patches_keeps_document(self) -> None:
        patch = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        decide_patch(patch, "reject")
        outcome = apply_patches(DOC, self.spans, [patch])
        self.assertEqual(outcome.document, DOC)
        self.assertEqual(outcome.applied, [])

    def test_patch_round_trip_json(self) -> None:
        patch = self.patch(self.abstract, "效果比较好", "表现更好", "patch-001")
        decide_patch(patch, "accept")
        payload = patch.to_json()
        restored = type(patch).from_json(payload)
        self.assertEqual(restored.patch_id, patch.patch_id)
        self.assertEqual(restored.status, "accepted")
        self.assertEqual(restored.position, patch.position)
        self.assertIn("status_label", payload)


class TestModelPayloadTolerance(unittest.TestCase):
    """模型输出字段名不统一时也要能解析。"""

    def setUp(self) -> None:
        self.spans = build_spans(DOC)
        self.abstract = next(span for span in self.spans if span.text.startswith("本文提出"))

    def test_find_replace_naming(self) -> None:
        patch = validate_patch(
            patch_from_model(
                {
                    "block_id": self.abstract.id,
                    "find": "效果比较好",
                    "replace": "表现更好",
                    "reason": "更具体",
                },
                spans=self.spans,
                patch_id="patch-001",
            ),
            self.spans,
        )
        self.assertEqual(patch.status, "proposed")
        self.assertEqual(patch.rationale, "更具体")

    def test_suggestion_as_replacement(self) -> None:
        patch = validate_patch(
            patch_from_model(
                {
                    "span_label": self.abstract.label,
                    "quote": "效果比较好",
                    "suggestion": "表现更好",
                },
                spans=self.spans,
                patch_id="patch-002",
            ),
            self.spans,
        )
        self.assertEqual(patch.status, "proposed")
        self.assertEqual(patch.replace, "表现更好")

    def test_sentence_markers_are_stripped_from_replacement(self) -> None:
        patch = validate_patch(
            patch_from_model(
                {
                    "span_label": self.abstract.label,
                    "quote": "效果比较好",
                    "replace": "(1) 本文提出了一种新方法。 (2) 该方法在三个数据集上均有提升。",
                    "rationale": "补充具体结论",
                },
                spans=self.spans,
                patch_id="patch-009",
            ),
            self.spans,
        )
        self.assertEqual(patch.status, "proposed")
        self.assertNotIn("(1)", patch.replace)
        self.assertNotIn("(2)", patch.replace)
        self.assertIn("本文提出了一种新方法。", patch.replace)
        self.assertTrue(any("句子编号" in warning for warning in patch.warnings))

    def test_author_own_numbering_is_kept(self) -> None:
        patch = validate_patch(
            patch_from_model(
                {
                    "span_label": self.abstract.label,
                    "quote": "效果比较好",
                    "replace": "改为（1）方法有效（2）效果稳定",
                    "rationale": "保留作者自己的编号",
                },
                spans=self.spans,
                patch_id="patch-010",
            ),
            self.spans,
        )
        self.assertIn("（1）", patch.replace)

    def test_strip_sentence_markers_directly(self) -> None:
        self.assertEqual(
            strip_sentence_markers("(1) 第一句。 (2) 第二句。"), "第一句。 第二句。"
        )
        self.assertEqual(strip_sentence_markers("(1) 只有一条"), "(1) 只有一条")


if __name__ == "__main__":
    unittest.main()
