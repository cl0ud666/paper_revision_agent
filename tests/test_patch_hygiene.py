"""
补丁卫生：**没有改动的补丁不该进用户的清单**。

模型有时会提一条 replace 与 quote 逐字相同的补丁（等于什么都没改）。
这类补丁以前会以"无法定位"的名义出现在清单里——那三个字会让人以为定位失败，
实际却是"根本没有改动可定位"。现在直接丢掉，只在日志里留一句计数。
"""

from __future__ import annotations

import unittest

from app.agents import revision as revision_agent
from app.document.patches import (
    CONFLICT_TEXT,
    Patch,
    STATUS_APPLIED,
    STATUS_CONFLICT,
    validate_patch,
)
from app.document.spans import build_spans


def noop_patch(patch_id: str = "patch-001") -> Patch:
    return Patch(
        patch_id=patch_id,
        span_id="p1234abcd",
        span_label="P116",
        target="span",
        quote="阈值为0.55至0.60，故取阈值为0.6。",
        replace="阈值为0.55至0.60，故取阈值为0.6。",
        status=STATUS_CONFLICT,
        conflict_code="no_change",
        conflict_reason=CONFLICT_TEXT["no_change"],
    )


class TestDropNoopPatches(unittest.TestCase):
    def test_noop_patch_is_dropped(self) -> None:
        real = Patch(
            patch_id="patch-002",
            span_id="p5678efgh",
            span_label="P120",
            quote="这句话有问题",
            replace="这句话改好了",
        )
        notes: list[str] = []
        kept = revision_agent._drop_noop_patches(
            [noop_patch(), real], notes, lambda message: None
        )

        self.assertEqual([patch.patch_id for patch in kept], ["patch-002"])
        self.assertEqual(len(notes), 1)
        self.assertIn("修改前后相同", notes[0])

    def test_other_conflicts_are_kept(self) -> None:
        """其它原因被拒的补丁仍然要出现在清单里——用户需要知道哪条没落下去。"""

        for reason in ("not_found", "ambiguous", "empty_replace", "overlap"):
            patch = Patch(
                patch_id=f"patch-{reason}",
                span_id="p1234abcd",
                span_label="P116",
                status=STATUS_CONFLICT,
                conflict_code=reason,
                conflict_reason=CONFLICT_TEXT[reason],
            )
            kept = revision_agent._drop_noop_patches(
                [patch], [], lambda message: None
            )
            self.assertEqual(len(kept), 1, f"{reason} 不该被丢掉")

    def test_nothing_dropped_means_no_note(self) -> None:
        notes: list[str] = []
        kept = revision_agent._drop_noop_patches([], notes, lambda message: None)
        self.assertEqual(kept, [])
        self.assertEqual(notes, [])


class TestConflictLabels(unittest.TestCase):
    """
    被拒补丁的短标签必须按**原因**细分。

    以前一律显示"无法定位"，但"改后为空""前后相同"根本不是定位问题——
    那三个字会让用户以为系统找不到位置，实际位置找得好好的。
    """

    def test_labels_by_reason(self) -> None:
        expected = {
            "not_found": "无法定位",
            "no_span": "无法定位",
            "empty": "无法定位",
            "ambiguous": "定位有歧义",
            "empty_replace": "无效改动",
            "no_change": "无效改动",
            "overlap": "改动冲突",
        }
        for code, label in expected.items():
            patch = Patch(
                patch_id="patch-001",
                span_id="p1234abcd",
                status=STATUS_CONFLICT,
                conflict_code=code,
            )
            self.assertEqual(patch.status_label, label, f"{code} 的标签不对")

    def test_unknown_reason_falls_back(self) -> None:
        patch = Patch(
            patch_id="patch-001", span_id="p1234abcd", status=STATUS_CONFLICT
        )
        self.assertEqual(patch.status_label, "无法定位")

    def test_normal_status_is_untouched(self) -> None:
        patch = Patch(patch_id="patch-001", span_id="p1234abcd", status=STATUS_APPLIED)
        self.assertEqual(patch.status_label, "已应用")

    def test_to_json_carries_the_specific_label(self) -> None:
        patch = Patch(
            patch_id="patch-001",
            span_id="p1234abcd",
            status=STATUS_CONFLICT,
            conflict_code="overlap",
        )
        payload = patch.to_json()
        self.assertEqual(payload["status_label"], "改动冲突")
        self.assertEqual(payload["conflict_code"], "overlap")

    def test_validate_patch_records_the_reason_code(self) -> None:
        """端到端：校验层判"前后相同"时，必须把原因代码一起记下来。"""

        spans = build_spans("# 标题\n\n这是一句正文。\n", version=1)
        span = next(s for s in spans if "这是一句正文" in s.text)
        patch = Patch(
            patch_id="patch-001",
            span_id=span.id,
            span_label=span.label,
            quote="这是一句正文。",
            replace="这是一句正文。",
        )
        out = validate_patch(patch, spans)

        self.assertEqual(out.status, STATUS_CONFLICT)
        self.assertEqual(out.conflict_code, "no_change")
        self.assertEqual(out.status_label, "无效改动")


if __name__ == "__main__":
    unittest.main()
