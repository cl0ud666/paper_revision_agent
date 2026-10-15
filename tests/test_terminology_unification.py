"""
术语统一：**口径由人定，执行由程序做**。

用户抱怨过的场景：说了"全文统一为 MOCO"，结果只有第一、二章改了，
后面几章还是 MoCo，而且没有任何说明。

根因是两层：

1. Revision 只改"被点名的问题"所在的**分块**，别处的 MoCo 它看不见；
2. 事后想从补丁反推"MoCo→MOCO"这条词级映射，也只能拿到整句片段。

所以改成：审计阶段由扫描直接给出**全部位置**（`span_labels`），
用户在闸门上选定口径后，**由程序**全文替换（穷尽、确定、零成本），
并且**代码段一律不碰**。
"""

from __future__ import annotations

import unittest

from app.agents.audit import _variation_issues
from app.agents.revision import _unification_patches
from app.document.spans import build_spans
from app.utils.consistency import find_naming_variants
from app.utils.text import looks_like_code

DOC = (
    "# 论文标题\n\n"
    "## 1 引言\n\n"
    "本文提出 MoCo 方法，MoCo 在实验中很有效。\n\n"
    "## 2 方法\n\n"
    "对比学习部分采用 MOCO 框架。\n\n"
    "## 4 实现\n\n"
    "class MoCo(nn.Module):\n"
    "    def __init__(self):\n"
    "        self.queue = torch.randn(EMBED_DIM, MOCO_QUEUE_SIZE)\n"
)


def rule(text: str) -> dict:
    return {"id": "R3", "kind": "rule", "status": "active", "text": text}


class TestVariationIssue(unittest.TestCase):
    """审计阶段：一条问题，带**全部**位置 + 由变体生成的可执行选项。"""

    def test_issue_carries_every_location_and_options(self) -> None:
        spans = build_spans(DOC, version=1)
        prose = [s for s in spans if not looks_like_code(s.text)]
        hint = find_naming_variants(prose)
        issues = _variation_issues(hint)

        self.assertEqual(len(issues), 1, "MoCo/MOCO 的不一致没被扫出来")
        issue = issues[0]

        self.assertEqual(issue["kind"], "decision", "术语统一必须由作者拍板")
        self.assertEqual(issue["source"], "naming_variants")
        # 选项要**可执行**：下游按"全文统一为 X"做确定性替换
        self.assertIn("全文统一为 MOCO", issue["options"])
        self.assertIn("全文统一为 MoCo", issue["options"])
        # 标号要是**全部**位置（不是只有一个代表）
        self.assertGreaterEqual(len(issue["span_labels"]), 2)

    def test_snake_case_identifiers_are_not_naming_variants(self) -> None:
        spans = build_spans(
            "变量 FEWSHOT_QUERY_DIR 用于查询。\n\n"
            "变量 fewshot_query_dir 仅在代码示例中出现。", version=1
        )
        self.assertEqual(find_naming_variants(spans), [])


class TestUnificationPatches(unittest.TestCase):
    """Revision 阶段：口径来自用户，执行由程序，代码段不碰。"""

    def setUp(self) -> None:
        self.spans = build_spans(DOC, version=1)

    def test_no_rule_means_no_patch(self) -> None:
        self.assertEqual(_unification_patches(self.spans, []), [])

    def test_replaces_every_occurrence_in_prose(self) -> None:
        patches = _unification_patches(self.spans, [rule("P01：全文统一为 MOCO")])

        quoted = [p.quote for p in patches]
        self.assertEqual(len(patches), 2, f"正文里的两处 MoCo 都该改：{quoted}")
        for patch in patches:
            self.assertIn("MOCO", patch.replace)
            self.assertNotIn("MoCo", patch.replace)
            self.assertEqual(patch.strategy, "unification")

    def test_code_is_never_touched(self) -> None:
        patches = _unification_patches(self.spans, [rule("P01：全文统一为 MOCO")])

        code_ids = {
            span.id for span in self.spans if looks_like_code(span.text)
        }
        self.assertTrue(code_ids, "样本里应当有代码段")
        for patch in patches:
            self.assertNotIn(
                patch.span_id, code_ids, "代码段被改了——用户明确要求代码一律不碰"
            )

    def test_quote_is_unique_inside_its_span(self) -> None:
        """同一段里出现两次时，quote 必须扩窗到唯一，否则补丁会被判歧义。"""

        patches = _unification_patches(self.spans, [rule("P01：全文统一为 MOCO")])
        by_label: dict[str, list[str]] = {}
        for patch in patches:
            by_label.setdefault(patch.span_label, []).append(patch.quote)

        spans = {span.label: span.text for span in self.spans}
        for label, quotes in by_label.items():
            for quote in quotes:
                self.assertEqual(
                    spans[label].count(quote), 1, f"{label} 的 quote 不唯一：{quote}"
                )


class TestPresentationMerge(unittest.TestCase):
    """呈现：47 条同一口径的补丁在确认清单里合成一条。"""

    def test_same_target_is_merged_into_one_entry(self) -> None:
        from app.workflow.nodes import _merge_unification_items

        items = [
            {
                "patch_id": f"patch-{i:03d}",
                "span_label": f"P{i:02d}",
                "strategy": "unification",
                "rationale": "按用户要求，全文统一为「MOCO」；此处「MoCo」改为「MOCO」",
            }
            for i in range(1, 48)
        ]
        merged = _merge_unification_items(items)

        self.assertEqual(len(merged), 1, "47 处同一口径应当合成一条")
        entry = merged[0]
        self.assertEqual(entry["count"], 47)
        self.assertEqual(entry["position"], "全文")
        self.assertIn("MOCO", entry["replace"])
        self.assertIn("P01", entry["span_labels"])
        self.assertIn("P47", entry["span_labels"])

    def test_other_patches_are_left_alone(self) -> None:
        from app.workflow.nodes import _merge_unification_items

        items = [
            {"patch_id": "patch-001", "span_label": "P03", "strategy": ""},
            {
                "patch_id": "patch-002",
                "span_label": "P09",
                "strategy": "unification",
                "rationale": "全文统一为「MOCO」",
            },
        ]
        merged = _merge_unification_items(items)

        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["patch_id"], "patch-001")  # 普通补丁不动
        self.assertEqual(merged[1]["count"], 1)


class TestLabelUnion(unittest.TestCase):
    """合并重复问题时，位置要取**并集**（分块报的位置 + 全篇补的位置）。"""

    def test_span_labels_are_unioned_when_duplicates_merge(self) -> None:
        from app.agents.audit import _merge_issues

        chunk_issue = {
            "kind": "decision",
            "category": "专有名词",
            "severity": "medium",
            "problem": "同一个名称在全文有两种写法，需要统一",
            "span_label": "P26",
            "span_labels": ["P26", "P28"],
        }
        whole_issue = {
            "kind": "decision",
            "category": "专有名词",
            "severity": "medium",
            "problem": "同一个名称在全文有两种写法，需要统一",
            "span_label": "P26",
            "span_labels": ["P26", "P145", "P300"],
        }
        merged = _merge_issues([chunk_issue, whole_issue])

        self.assertEqual(len(merged), 1, "说的是同一件事就该合并成一条")
        self.assertEqual(
            merged[0]["span_labels"], ["P26", "P28", "P145", "P300"]
        )


if __name__ == "__main__":
    unittest.main()
