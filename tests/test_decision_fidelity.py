"""Regression tests for user choices, manual work and Word export failures."""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.agents import report, revision
from app.document.docx_model import DocxSource
from app.document.docx_redline import export_redline
from app.document.final_review import visible_unfinished
from app.document.preview import prepare_visual_review
from app.document.review_items import public_item
from app.document.spans import build_spans
from app.workflow.interaction import prepare_response
from app.workflow.nodes import (
    _confirm_before_writing,
    apply_node,
    issue_review_node,
    normalize_issue_payload,
)
from tests.test_final_review import fake_pages, state_for
from tests.test_web_api import paper_bytes


class TestDecisionFidelity(unittest.TestCase):
    def choice(self, document):
        span = build_spans(document)[0]
        issue = {
            "id": 7,
            "kind": "decision",
            "span_label": span.label,
            "span_id": span.id,
            "problem": "BERT 比较缺少实验依据",
            "evidence": span.text,
            "suggestion": "删除 BERT 描述",
            "options": ["删除 BERT 描述", "补充 BERT 对比实验，保留描述"],
        }
        request = {"type": "issue_review", "issues": [issue]}
        answer = prepare_response(request, {"choices": {"7": 1}})
        # API validation + worker resume both normalize the answer.
        again = prepare_response(request, answer)
        self.assertEqual(answer, again)
        return normalize_issue_payload(again, [issue])

    def test_choice_b_never_becomes_deletion_even_with_wrong_issue_id(self):
        state = state_for("与 BERT 相比，本方法准确率更高。\n", [])
        state.update(self.choice(state["original_document"]))
        state.update(user_request="全面修改")
        from types import SimpleNamespace

        with patch(
            "app.agents.revision.agent.call_llm",
            return_value=SimpleNamespace(text='{"patches":[]}'),
        ):
            update = revision.run(state, logger=lambda _: None)
        self.assertFalse(update["proposed_patches"])
        span = build_spans(state["current_document"])[0]
        state["proposed_patches"] = [
            {
                "patch_id": "wrong",
                "span_id": span.id,
                "span_label": span.label,
                "issue_id": 99,
                "quote": span.text,
                "delete": True,
                "replace": "",
                "status": "accepted",
            }
        ]
        update = apply_node(state, logger=lambda _: None)
        self.assertFalse(update.get("applied_patches"))
        self.assertIn("未授权删除", update["conflict_patches"][0]["conflict_reason"])

    def test_missing_options_skip_choice_gate_and_survive_as_manual(self):
        issue = {"id": 1, "kind": "decision", "problem": "需要作者核对实验事实", "options": []}
        with patch("app.workflow.nodes.interrupt", side_effect=AssertionError("empty choice gate")):
            result = issue_review_node({"audit_findings": [issue]}, logger=lambda _: None)
        self.assertTrue(result["audit_findings"][0]["manual_required"])
        self.assertEqual(issue["kind"], "decision")  # no mutation of input checkpoint

    def test_manual_citation_is_shown_before_confirmation_and_written_as_comment(self):
        data = paper_bytes()
        original = DocxSource(data).text
        state = {
            "visual_review": True,
            "run_id": "manual-test",
            "original_document": original,
            "current_document": original,
            "user_request": "请添加引用",
            "audit_findings": [],
        }
        from types import SimpleNamespace

        # Missing-source advice now reaches Revision; mock a real failure rather
        # than calling a live model from the regression suite.
        with patch(
            "app.agents.revision.agent.call_llm",
            return_value=SimpleNamespace(
                text='{"patches":[],"unfulfilled":[{"issue_id":1,"reason":"缺少可核验的候选文献资料","suggestion":"请提供文献来源"}]}'
            ),
        ):
            update = revision.run(state, logger=lambda _: None)
        self.assertEqual(len(update["manual_items"]), 1)
        state.update(update)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.docx"
            source.write_bytes(data)
            state["source_docx"] = str(source)
            with patch("app.document.preview.render_pages", side_effect=fake_pages):
                state.update(prepare_visual_review(state, folder))
            state["final_review_ready"] = True
            seen = []

            def confirm(request):
                seen.append(request)
                return {"excluded_ids": []}

            with patch("app.workflow.nodes.interrupt", side_effect=confirm):
                state = _confirm_before_writing(state, lambda _: None)
            self.assertEqual(len(seen[0]["manual_items"]), 1)
            result = report.write_redline_docx(state, run_dir=Path(folder), logger=lambda _: None)
            self.assertEqual(result["current_document"], original)
            with zipfile.ZipFile(result["docx_path"]) as archive:
                comments = archive.read("word/comments.xml").decode()
            self.assertIn("无法修改，需人工修改", comments)
            self.assertIn("添加引用", comments)
            visible = visible_unfinished(
                result["unfinished_items"],
                [],
                [m["manual_id"] for m in state["final_manual_items"]],
            )
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible_unfinished(result["unfinished_items"], [], []), [])

    def test_late_write_failure_has_one_manual_comment_and_result_item(self):
        data = paper_bytes()
        state = state_for(DocxSource(data).text, [("MoCo", "MOCO")])
        state.update(visual_review=True)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.docx"
            source.write_bytes(data)
            state["source_docx"] = str(source)
            with patch("app.document.preview.render_pages", side_effect=fake_pages):
                state.update(prepare_visual_review(state, folder))
            state.update(final_review_ready=True, final_review_confirmed=True)
            with patch(
                "app.document.docx_redline.RedlineWriter._replace",
                side_effect=ValueError("模拟结构不支持"),
            ):
                result = report.write_redline_docx(
                    state, run_dir=Path(folder), logger=lambda _: None
                )
            self.assertEqual(result["redline_summary"]["comments"], 1)
            self.assertEqual(result["redline_summary"]["applied"], 0)
            self.assertEqual(result["unfinished_items"][0]["kind"], "blocked")
            with zipfile.ZipFile(result["docx_path"]) as archive:
                comments = archive.read("word/comments.xml").decode()
            self.assertIn("模拟结构不支持", comments)
            self.assertIn("修改建议", comments)

    def test_user_locations_use_actual_heading_and_never_fabricate_pages(self):
        document = "## 2.2.3 动量更新与队列管理机制\n\n正文方法描述。\n"
        span = build_spans(document)[1]
        display = public_item(
            {"span_label": span.label, "problem": f"{span.label} 与 H999 冲突"}, document
        )
        self.assertIn("2.2.3 动量更新与队列管理机制", display["position"])
        self.assertNotIn(span.label, display["problem"])
        self.assertNotIn("H999", display["problem"])
        self.assertNotIn("页", display["position"])

    def test_whole_export_error_restarts_from_source_with_manual_comments(self):
        data = paper_bytes()
        state = state_for(DocxSource(data).text, [("MoCo", "MOCO")])
        state.update(visual_review=True)
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.docx"
            source.write_bytes(data)
            state["source_docx"] = str(source)
            with patch("app.document.preview.render_pages", side_effect=fake_pages):
                state.update(prepare_visual_review(state, folder))
            state.update(final_review_ready=True, final_review_confirmed=True)

            def fail_first(data, rounds, target, **kwargs):
                if any(rounds):
                    raise ValueError("结构写回异常")
                return export_redline(data, rounds, target, **kwargs)

            with patch("app.agents.report.export_redline", side_effect=fail_first):
                result = report.write_redline_docx(
                    state, run_dir=Path(folder), logger=lambda _: None
                )
            self.assertEqual(result["current_document"], state["original_document"])
            self.assertFalse(result["redline_summary"].get("error"))
            self.assertEqual(result["redline_summary"]["comments"], 1)
            self.assertEqual(result["unfinished_items"][0]["kind"], "blocked")
            with zipfile.ZipFile(result["docx_path"]) as archive:
                self.assertIn("结构写回异常", archive.read("word/comments.xml").decode())
