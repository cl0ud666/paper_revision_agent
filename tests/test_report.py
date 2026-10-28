"""
报告生成测试：多轮修改都要出现在报告与产物里。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents.report import build_report, load_state_snapshot, write_outputs
from app.config import reset_settings
from app.document.history import all_applied_edits, all_failed_edits
from app.utils.tracing import reset_recorders


def sample_state() -> dict:
    return {
        "run_id": "report-test",
        "user_request": "全面检查并修改这篇论文。",
        "original_document": "原文档内容。",
        "current_document": "修改后的文档内容。",
        "plan_kind": "replan",
        "next_action": "revision",
        "plan": ["先审查", "再修改"],
        "plan_reason": "测试用理由",
        "focus_areas": ["语言表达"],
        "iteration": 2,
        "max_iterations": 3,
        "audit_findings": [
            {
                "id": 1,
                "category": "语言表达",
                "severity": "high",
                "location": "摘要",
                "problem": "表述笼统",
                "approved": True,
            }
        ],
        "approved_issue_ids": [1],
        "human_constraints": ["不要修改实验结果"],
        "human_feedback": "",
        "human_decision": "approve",
        "human_rounds": [{"iteration": 0, "decision": "approve", "approved": [1]}],
        "revision_summary": "第二轮摘要",
        "patch_stats": {"proposed": 0, "accepted": 2, "rejected": 1, "applied": 2, "conflict": 0},
        "proposed_patches": [],
        "applied_patches": [
            {
                "patch_id": "patch-001",
                "issue_id": 1,
                "round": 1,
                "span_label": "P01",
                "before": "第一轮旧文",
                "replace": "第一轮新文",
                "rationale": "把笼统表述改具体",
                "decided_by": "user",
                "status": "applied",
            },
            {
                "patch_id": "patch-002",
                "issue_id": 1,
                "round": 2,
                "span_label": "P01",
                "before": "第二轮旧文",
                "replace": "第二轮新文",
                "rationale": "补上实验设置",
                "decided_by": "user",
                "status": "applied",
            },
        ],
        "rejected_patches": [
            {
                "patch_id": "patch-003",
                "issue_id": 2,
                "round": 2,
                "span_label": "P02",
                "before": "被拒的旧文",
                "replace": "被拒的新文",
                "rationale": "改成更学术的表述",
                "decided_by": "user",
                "status": "rejected",
            }
        ],
        "conflict_patches": [],
        "budget": {
            "used_tokens": 12345,
            "max_run_tokens": 400000,
            "elapsed_seconds": 42.0,
            "max_run_seconds": 1800.0,
            "calls_per_agent": {"audit": 1, "revision": 2, "review": 2},
            "retries": 0,
            "failures": 0,
            "exceeded": False,
            "reason": "",
        },
        "revision_edits": [
            {"issue_id": 1, "original": "第二轮旧文", "revised": "第二轮新文", "strategy": "exact"}
        ],
        "revision_failed_edits": [{"issue_id": 2, "reason": "not_found", "find": "找不到的句子"}],
        "revision_history": [
            {
                "iteration": 1,
                "edits": 1,
                "failed": 0,
                "summary": "第一轮摘要",
                "applied": [
                    {
                        "issue_id": 1,
                        "original": "第一轮旧文",
                        "revised": "第一轮新文",
                        "strategy": "normalized",
                    }
                ],
                "failed_edits": [],
            },
            {
                "iteration": 2,
                "edits": 1,
                "failed": 1,
                "summary": "第二轮摘要",
                "applied": [
                    {
                        "issue_id": 1,
                        "original": "第二轮旧文",
                        "revised": "第二轮新文",
                        "strategy": "exact",
                    }
                ],
                "failed_edits": [{"issue_id": 2, "reason": "not_found", "find": "找不到的句子"}],
            },
        ],
        "review_result": {
            "status": "fail",
            "summary": "仍有问题",
            "remaining_issues": [
                {"id": 2, "category": "实验分析", "severity": "medium", "problem": "分析不足"}
            ],
            "constraint_violations": [],
            "dimension_scores": {"语法": 4},
            "confidence": 0.8,
            "checks": [{"name": "文档非空", "status": "ok", "detail": "ok"}],
        },
        "audit_meta": {"chunks": 1, "raw_issues": 2, "merged_issues": 1},
        "errors": ["Audit 某个片段失败"],
        "trace": [],
    }


class TestReport(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-report-"))
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        reset_settings()
        reset_recorders()

    def tearDown(self) -> None:
        os.environ.pop("PRA_RUNS_DIR", None)
        reset_settings()
        reset_recorders()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_edits_from_all_rounds(self) -> None:
        state = sample_state()
        edits = all_applied_edits(state)
        self.assertEqual(len(edits), 2)
        self.assertEqual([edit["round"] for edit in edits], [1, 2])
        self.assertEqual(len(all_failed_edits(state)), 1)

    def test_report_contains_all_rounds(self) -> None:
        report = build_report(sample_state())
        self.assertIn("第一轮新文", report)
        self.assertIn("第二轮新文", report)
        self.assertIn("各轮执行情况", report)
        self.assertIn("修改理由（由Agent给出）", report)
        self.assertIn("把笼统表述改具体", report)
        self.assertIn("被拒绝的改动", report)
        self.assertIn("成本与预算", report)
        self.assertIn("FAIL", report)
        self.assertIn("仍然存在的问题", report)
        self.assertIn("异常与降级记录", report)
        self.assertIn("复核未通过", report)

    def test_report_marks_iteration_cap(self) -> None:
        state = sample_state()
        state["max_iterations"] = 2
        report = build_report(state)
        self.assertIn("迭代上限", report)

    def test_write_outputs_creates_files(self) -> None:
        state = sample_state()
        trace_summary = {
            "run_id": "report-test",
            "per_agent": {
                "audit": {
                    "calls": 1,
                    "failures": 0,
                    "retries": 0,
                    "total_ms": 10.0,
                    "total_tokens": 100,
                }
            },
            "totals": {
                "calls": 1,
                "failures": 0,
                "retries": 0,
                "total_ms": 10.0,
                "total_tokens": 100,
            },
        }
        update = write_outputs(state, trace_summary=trace_summary, logger=lambda message: None)

        for key in ("report_path", "document_path", "edits_path", "state_path"):
            self.assertTrue(Path(update[key]).exists(), key)

        edits = json.loads(Path(update["edits_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(edits["applied_all_rounds"]), 2)
        self.assertEqual(len(edits["history"]), 2)

        snapshot = load_state_snapshot(update["state_path"])
        self.assertNotIn("trace", snapshot)
        self.assertEqual(snapshot["iteration"], 2)

    def test_renders_existing_run_snapshot(self) -> None:
        """兼容性检查：改版前保存的状态快照也要能渲染出报告。"""

        legacy = {
            "run_id": "legacy",
            "user_request": "旧任务",
            "current_document": "内容",
            "audit_findings": [],
            "revision_edits": [
                {"issue_id": 1, "original": "旧", "revised": "新", "strategy": "exact"}
            ],
            "revision_history": [{"iteration": 1, "edits": 1, "failed": 0, "summary": "旧摘要"}],
            "review_result": {"status": "pass"},
        }
        report = build_report(legacy)
        self.assertIn("新", report)
        self.assertIn("复核通过", report)


if __name__ == "__main__":
    unittest.main()
