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
        self.assertIn("delete", patch.conflict_reason)


if __name__ == "__main__":
    unittest.main()
