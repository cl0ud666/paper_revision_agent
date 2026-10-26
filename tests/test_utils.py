"""
工具层单元测试：JSON 解析、编辑落地、文本处理、Context 治理、重试熔断。
"""

from __future__ import annotations

import time
import unittest

from app.utils.context import build_context, fit_budget, format_context
from app.utils.edits import apply_edits, find_span, normalize_for_match
from app.utils.json_utils import (
    ModelOutputError,
    compact_json,
    extract_json,
    parse_json_list,
    parse_json_object,
)
from app.utils.resilience import (
    AgentGuard,
    AgentTimeoutError,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
)
from app.utils.text import (
    document_outline,
    estimate_tokens,
    similarity,
    split_chunks,
    split_sections,
    truncate_middle,
)
from app.utils.tracing import TraceRecorder


class TestJsonUtils(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence(self) -> None:
        text = '好的，结果如下：\n```json\n{"status": "pass"}\n```\n希望有帮助。'
        self.assertEqual(extract_json(text), {"status": "pass"})

    def test_prose_wrapped(self) -> None:
        text = '分析如下 {"issues": [{"id": 1}]} 以上。'
        self.assertEqual(extract_json(text), {"issues": [{"id": 1}]})

    def test_trailing_comma_and_python_literals(self) -> None:
        text = "{'a': True, 'b': [1, 2,], }"
        self.assertEqual(extract_json(text), {"a": True, "b": [1, 2]})

    def test_braces_inside_strings(self) -> None:
        text = '前缀 {"problem": "使用 {花括号} 说明", "id": 2} 后缀'
        self.assertEqual(extract_json(text), {"problem": "使用 {花括号} 说明", "id": 2})

    def test_curly_quotes(self) -> None:
        text = "{\u201cstatus\u201d: \u201cfail\u201d}"
        self.assertEqual(extract_json(text), {"status": "fail"})

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ModelOutputError):
            extract_json("完全不是 JSON")

    def test_required_keys(self) -> None:
        with self.assertRaises(ModelOutputError):
            parse_json_object('{"other": 1}', required_keys=("issues",), context="测试")

    def test_wrapped_object(self) -> None:
        data = parse_json_object('{"result": {"issues": []}}', required_keys=("issues",))
        self.assertEqual(data, {"issues": []})

    def test_parse_list_from_object(self) -> None:
        self.assertEqual(parse_json_list('{"issues": [1, 2]}', key="issues"), [1, 2])

    def test_compact_json_truncates(self) -> None:
        text = compact_json({"a": "x" * 100}, max_chars=20)
        self.assertLess(len(text), 40)
        self.assertIn("截断", text)


class TestEdits(unittest.TestCase):
    def test_exact_unique_match(self) -> None:
        document = "第一句。第二句很好。第三句。"
        result = apply_edits(
            document,
            [{"issue_id": 1, "find": "第二句很好。", "replace": "第二句非常好。"}],
        )
        self.assertEqual(result.document, "第一句。第二句非常好。第三句。")
        self.assertEqual(result.applied[0]["strategy"], "exact")
        self.assertEqual(result.failed, [])

    def test_multiple_matches_uses_location_hint(self) -> None:
        document = "重复片段A。中间内容。重复片段A。"
        result = apply_edits(
            document,
            [
                {
                    "issue_id": 1,
                    "find": "重复片段A。",
                    "replace": "改后片段。",
                    "location": "中间内容。重复片段A。",
                }
            ],
        )
        self.assertEqual(result.document, "重复片段A。中间内容。改后片段。")
        self.assertEqual(result.applied[0]["strategy"], "exact-nearest")

    def test_normalized_match_ignores_whitespace(self) -> None:
        document = "本文  提出了一个新的方法，\n效果比较好。"
        result = apply_edits(
            document,
            [{"issue_id": 1, "find": "本文提出了一个新的方法，效果比较好。", "replace": "改后。"}],
        )
        self.assertEqual(result.document, "改后。")
        self.assertEqual(result.applied[0]["strategy"], "normalized")

    def test_fuzzy_paragraph_match(self) -> None:
        document = "第一段正常内容。\n\n本文提出了一个新的方法，效果还算可以吧。\n\n第三段。"
        result = apply_edits(
            document,
            [
                {
                    "issue_id": 1,
                    "find": "本文提出了一个新的方法，效果还算可以。",
                    "replace": "本文提出了一种新方法，效果优于基线。",
                }
            ],
        )
        self.assertIn("优于基线", result.document)
        self.assertEqual(result.applied[0]["strategy"], "fuzzy-paragraph")

    def test_not_found_goes_to_failed(self) -> None:
        result = apply_edits(
            "原文内容。", [{"issue_id": 3, "find": "不存在的句子。", "replace": "x"}]
        )
        self.assertEqual(result.applied, [])
        self.assertEqual(result.failed[0]["reason"], "not_found")
        self.assertEqual(result.document, "原文内容。")

    def test_ambiguous_match_is_rejected(self) -> None:
        document = "前半句一样。\n\n后半句完全不同的一段文字。"
        start, end, strategy = find_span(document, "前半句一样。）不一样的")
        self.assertLess(start, 0)

    def test_empty_replacement_is_deletion(self) -> None:
        result = apply_edits("保留。删除我。", [{"issue_id": 1, "find": "删除我。", "replace": ""}])
        self.assertEqual(result.document, "保留。")

    def test_no_change_is_skipped(self) -> None:
        result = apply_edits("内容。", [{"issue_id": 1, "find": "内容。", "replace": "内容。"}])
        self.assertEqual(result.skipped[0]["reason"], "no_change")

    def test_sequential_edits(self) -> None:
        document = "A句。B句。"
        result = apply_edits(
            document,
            [
                {"issue_id": 1, "find": "A句。", "replace": "A句改。"},
                {"issue_id": 2, "find": "B句。", "replace": "B句改。"},
            ],
        )
        self.assertEqual(result.document, "A句改。B句改。")
        self.assertEqual(len(result.applied), 2)

    def test_normalize_for_match(self) -> None:
        self.assertEqual(normalize_for_match("A， B。\nC"), "A,B.C")


class TestText(unittest.TestCase):
    def test_estimate_tokens(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("中文四字"), 4)
        self.assertGreater(estimate_tokens("hello world"), 1)

    def test_split_sections_markdown(self) -> None:
        text = "# 标题\n\n## 摘要\n\n摘要内容。\n\n## 1 引言\n\n引言内容。\n"
        sections = split_sections(text)
        titles = [section.title for section in sections]
        self.assertIn("## 摘要", titles)
        self.assertIn("## 1 引言", titles)

    def test_split_sections_numbered(self) -> None:
        text = "1 引言\n\n内容一。\n\n2 方法\n\n内容二。\n"
        sections = split_sections(text)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[1].title, "2 方法")

    def test_split_chunks_respects_budget(self) -> None:
        text = "\n\n".join(["这是一个测试段落。" * 20] * 6)
        chunks = split_chunks(text, max_tokens=300)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLess(estimate_tokens(chunk), 600)

    def test_split_chunks_max_chunks(self) -> None:
        text = "\n\n".join(["段落内容。" * 30] * 10)
        chunks = split_chunks(text, max_tokens=200, max_chunks=3)
        self.assertEqual(len(chunks), 3)

    def test_truncate_middle(self) -> None:
        text = "开头" + "中间" * 500 + "结尾"
        truncated = truncate_middle(text, 50)
        self.assertIn("省略", truncated)
        self.assertTrue(truncated.startswith("开头"))
        self.assertTrue(truncated.endswith("结尾"))

    def test_truncate_middle_keeps_short_text(self) -> None:
        self.assertEqual(truncate_middle("短文本", 100), "短文本")

    def test_document_outline(self) -> None:
        outline = document_outline("# 标题\n\n## 摘要\n\n摘要内容。\n")
        self.assertIn("摘要", outline)

    def test_similarity(self) -> None:
        self.assertGreater(similarity("同样的句子", "同样的句子！"), 0.8)
        self.assertLess(similarity("完全不同", "abc"), 0.5)


class TestContext(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "user_request": "全面检查并修改论文",
            "current_document": "# 标题\n\n## 摘要\n\n摘要内容。\n\n## 1 引言\n\n引言内容。\n",
            "iteration": 1,
            "max_iterations": 3,
            "audit_findings": [{"id": 1, "problem": "问题一", "category": "语言表达"}],
            "approved_issue_ids": [1],
            "human_constraints": ["不要修改实验结果"],
            "plan": ["第一步"],
            "plan_reason": "测试",
            "review_result": {"status": "fail", "remaining_issues": [{"id": 1}]},
        }

    def test_orchestrator_context_has_no_chunk(self) -> None:
        fields = build_context(self.state, "orchestrator")
        self.assertIn("document_outline", fields)
        self.assertNotIn("chunk_text", fields)
        self.assertNotIn("approved_issues", fields)

    def test_audit_context_contains_chunk(self) -> None:
        fields = build_context(
            self.state,
            "audit",
            extra={"chunk_text": "片段内容", "chunk_index": 0, "chunk_total": 2},
        )
        self.assertEqual(fields["chunk_text"], "片段内容")
        self.assertNotIn("human_constraints", fields)

    def test_revision_context_contains_constraints(self) -> None:
        fields = build_context(self.state, "revision", extra={"chunk_text": "片段"})
        self.assertIn("不要修改实验结果", fields["user_rules"])
        self.assertIn("问题一", fields["approved_issues"])

    def test_user_rules_are_shared_with_all_agents(self) -> None:
        """用户定的要求，四个 Agent 都要看得到（否则 Audit 会把它当问题报）。"""

        state = dict(self.state)
        state["user_rules"] = [
            {
                "id": "R1",
                "kind": "rule",
                "text": "全文统一把「土豆」写成「马铃薯」",
                "status": "active",
                "created_turn": 1,
            },
            {
                "id": "R2",
                "kind": "ignore",
                "text": "不改 P05：参考文献格式",
                "signature": {"span_label": "P05"},
                "status": "active",
                "created_turn": 1,
            },
        ]
        state["audit_findings"] = [
            {"id": 1, "problem": "参考文献格式不统一", "span_label": "P05", "ignored": True}
        ]

        for agent in ("orchestrator", "audit", "revision", "review"):
            fields = build_context(state, agent)
            text = fields.get("user_rules", "")
            self.assertIn("马铃薯", text, agent)
            if agent != "orchestrator":
                self.assertIn("不改", fields.get("ignored_issues", ""), agent)

    def test_legacy_constraints_still_visible(self) -> None:
        fields = build_context(self.state, "revision", extra={"chunk_text": "片段"})
        self.assertIn("不要修改实验结果", fields["user_rules"])

    def test_budget_is_respected(self) -> None:
        big_state = dict(self.state)
        big_state["current_document"] = "长文本内容。" * 5000
        fields = build_context(big_state, "audit", budget_tokens=500)
        total = sum(estimate_tokens(value) for value in fields.values())
        self.assertLess(total, 900)

    def test_fit_budget_trims_large_field(self) -> None:
        fields = {"chunk_text": "内容" * 2000, "user_request": "请求"}
        trimmed = fit_budget(fields, 300)
        self.assertLess(estimate_tokens(trimmed["chunk_text"]), 2000)

    def test_format_context(self) -> None:
        text = format_context({"user_request": "请求内容"})
        self.assertIn("用户任务", text)
        self.assertIn("请求内容", text)


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestResilience(unittest.TestCase):
    def test_retry_then_success(self) -> None:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("临时失败")
            return "ok"

        recorder = TraceRecorder("test-run")
        guard = AgentGuard(
            name="audit",
            retry=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0),
            breaker=CircuitBreaker("audit", failure_threshold=10),
            default_timeout=5.0,
            sleep=lambda seconds: None,
        )
        result = guard.run(flaky, recorder=recorder, run_id="test-run")

        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)
        events = recorder.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].attempts, 3)
        self.assertTrue(events[0].ok)
        self.assertEqual(events[0].agent, "audit")

    def test_failure_after_all_attempts(self) -> None:
        guard = AgentGuard(
            name="audit",
            retry=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=0.0),
            breaker=CircuitBreaker("audit", failure_threshold=10),
            default_timeout=5.0,
            sleep=lambda seconds: None,
        )
        with self.assertRaises(RuntimeError):
            guard.run(lambda: (_ for _ in ()).throw(RuntimeError("一直失败")))

    def test_timeout(self) -> None:
        guard = AgentGuard(
            name="revision",
            retry=RetryPolicy(max_attempts=3, base_delay=0.0),
            breaker=CircuitBreaker("revision", failure_threshold=10),
            default_timeout=0.1,
        )
        start = time.perf_counter()
        with self.assertRaises(AgentTimeoutError):
            guard.run(lambda: time.sleep(1.0))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_circuit_opens_after_threshold(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker(
            "orchestrator", failure_threshold=2, recovery_timeout=60.0, clock=clock
        )
        guard = AgentGuard(
            name="orchestrator",
            retry=RetryPolicy(max_attempts=1),
            breaker=breaker,
            default_timeout=5.0,
        )

        def boom() -> None:
            raise RuntimeError("失败")

        for _ in range(2):
            with self.assertRaises(RuntimeError):
                guard.run(boom)

        self.assertEqual(breaker.state, "open")
        # 熔断期间快速失败，不再真正调用
        with self.assertRaises(CircuitOpenError):
            guard.run(lambda: "不应该执行")

        # 冷却结束后允许半开试探，成功后恢复
        clock.advance(61.0)
        self.assertEqual(guard.run(lambda: "恢复"), "恢复")
        self.assertEqual(breaker.state, "closed")

    def test_half_open_failure_reopens(self) -> None:
        clock = FakeClock()
        breaker = CircuitBreaker("audit", failure_threshold=1, recovery_timeout=10.0, clock=clock)
        guard = AgentGuard(
            name="audit",
            retry=RetryPolicy(max_attempts=1),
            breaker=breaker,
            default_timeout=5.0,
        )
        with self.assertRaises(RuntimeError):
            guard.run(lambda: (_ for _ in ()).throw(RuntimeError("失败")))
        self.assertEqual(breaker.state, "open")

        clock.advance(11.0)
        with self.assertRaises(RuntimeError):
            guard.run(lambda: (_ for _ in ()).throw(RuntimeError("仍然失败")))
        self.assertEqual(breaker.state, "open")


if __name__ == "__main__":
    unittest.main()
