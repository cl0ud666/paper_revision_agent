"""
结构还原与分区测试：论文不能被读成"一团没结构的文字流"。

模拟一份典型的中文毕业论文：封面字段 → 诚信承诺 → 摘要 → 目录（条目带页码）
→ 正文（编号标题）→ 参考文献 → 致谢。
"""

from __future__ import annotations

import unittest

from app.document.spans import (
    build_spans,
    select_auditable,
    structure_report,
)
from app.utils.text import is_heading

THESIS = """本 科 毕 业 设 计（论 文）

题    目

云服务器漏洞利用检测技术研究

学    号

22270606

指导教师

许艳萍

诚 信 承 诺

我谨在此承诺：本人所写的毕业论文均系本人独立完成。

承诺人（签名）：

摘    要

随着云技术的快速发展，云环境的安全问题也随之凸显。本文提出一种基于对比学习的方法。

关键词：对比学习；原型网络

目    录

1 引言 ................................ 1

1.1 研究背景与意义 .................... 1

2 相关工作 ............................ 5

1 引言

随着云计算的发展，云服务器面临着越来越多的安全威胁。

1.1 研究背景与意义

漏洞利用检测是云安全的核心问题之一，传统方法依赖特征工程。

2 相关工作

现有工作主要分为两类。

参考文献

[1] Kipf T N. Semi-supervised classification. ICLR, 2017.

致    谢

感谢导师在论文写作过程中给予的指导。
"""


class TestZoneClassification(unittest.TestCase):
    def setUp(self) -> None:
        self.spans = build_spans(THESIS)
        self.report = structure_report(self.spans)

    def test_spaced_headings_are_recognised(self) -> None:
        """'摘    要' / '目    录' / '致    谢' 被人为拉开字距，也要认出来。"""

        self.assertTrue(is_heading("摘    要"))
        self.assertTrue(is_heading("目    录"))
        self.assertTrue(is_heading("致    谢"))

    def test_zones_are_separated(self) -> None:
        zones = self.report["zones"]
        self.assertIn("cover", zones)
        self.assertIn("toc", zones)
        self.assertIn("abstract", zones)
        self.assertIn("body", zones)
        self.assertIn("references", zones)
        self.assertIn("acknowledgement", zones)

    def test_toc_entries_are_not_body(self) -> None:
        """目录条目长得像章节标题，但绝不能混进正文。"""

        toc_labels = {span.label for span in self.spans if span.zone == "toc"}
        body_text = " ".join(span.text for span in self.spans if span.zone == "body")

        self.assertTrue(toc_labels)
        self.assertNotIn("....................", body_text)
        self.assertNotIn("2 相关工作 ............................ 5", body_text)

    def test_cover_fields_are_excluded_from_audit(self) -> None:
        auditable = select_auditable(self.spans)
        text = " ".join(span.text for span in auditable)

        self.assertNotIn("22270606", text)      # 学号
        self.assertNotIn("许艳萍", text)        # 指导教师
        self.assertNotIn("诚 信 承 诺", text)
        self.assertNotIn("承诺人（签名）", text)
        self.assertNotIn("感谢导师", text)      # 致谢
        self.assertNotIn("[1] Kipf", text)      # 参考文献（默认不审）

    def test_body_and_abstract_are_audited(self) -> None:
        auditable = select_auditable(self.spans)
        text = " ".join(span.text for span in auditable)

        self.assertIn("随着云技术的快速发展", text)
        self.assertIn("随着云计算的发展", text)
        self.assertIn("现有工作主要分为两类", text)

    def test_scope_can_include_references(self) -> None:
        auditable = select_auditable(self.spans, "body+refs")
        text = " ".join(span.text for span in auditable)
        self.assertIn("[1] Kipf", text)

    def test_noise_blocks_are_skipped(self) -> None:
        skipped = [span for span in self.spans if not span.auditable]
        reasons = {span.no_reason for span in skipped}
        self.assertTrue(reasons & {"目录行", "签名/日期行", "页码"})

    def test_plain_text_without_headings_still_auditable(self) -> None:
        """没有标题的纯文本（例如直接贴一段内容）不能被整篇判成前置材料。"""

        plain = "这是一段没有标题的论文正文。" * 20
        spans = build_spans(plain)
        self.assertTrue(select_auditable(spans))

    def test_structure_report_counts(self) -> None:
        self.assertGreater(self.report["headings"], 3)
        self.assertGreater(self.report["auditable"], 2)
        self.assertLess(self.report["auditable"], self.report["blocks"])


if __name__ == "__main__":
    unittest.main()
