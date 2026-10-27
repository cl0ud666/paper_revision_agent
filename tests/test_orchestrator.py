from types import SimpleNamespace
from unittest.mock import patch

from app.agents import orchestrator
from app.state.schema import initial_state


def test_fallback_does_not_route_explicitly_negated_review_requests_to_audit():
    for request in (
        "无需全面审查，帮我润色摘要",
        "不需要审阅全文，直接修改摘要",
        "I don't need an audit, just revise the abstract",
    ):
        state = {
            "current_document": "论文正文。",
            "user_request": request,
        }

        assert not orchestrator.requests_full_review(request)
        assert orchestrator.fallback_plan(state)["next_action"] == "revision"


def test_fallback_keeps_deterministic_review_keyword_routing():
    result = orchestrator.fallback_plan(
        {"current_document": "论文正文。", "user_request": "请审查论文"}
    )

    assert result["next_action"] == "audit"


def test_positive_review_clause_is_not_suppressed_by_negated_clause():
    for request in (
        "不要审查摘要，但请全面校订全文",
        "全面检查论文并且不需要检查格式",
    ):
        assert orchestrator.requests_full_review(request)
        assert (
            orchestrator.fallback_plan({"current_document": "论文正文。", "user_request": request})[
                "next_action"
            ]
            == "audit"
        )


def test_negated_full_review_with_chinese_scope_marker_stays_direct_revision():
    request = "不要对全文进行审查，只修改摘要"

    assert not orchestrator.requests_full_review(request)
    assert (
        orchestrator.fallback_plan({"current_document": "论文正文。", "user_request": request})[
            "next_action"
        ]
        == "revision"
    )


def test_valid_planner_output_is_preserved_for_noncomprehensive_review_wording():
    result = orchestrator._normalize(
        {"next_action": "revision"},
        {
            "current_document": "论文正文。",
            "user_request": "review 摘要并修改这一段",
        },
        is_replan=False,
        log=lambda _: None,
    )

    assert result["next_action"] == "revision"


def test_plan_renders_the_actual_conversation_turn():
    state = initial_state("修改摘要", "论文正文。")
    state["turn"] = 4

    class ImmediateGuard:
        def run(self, fn, **kwargs):
            return fn()

    with (
        patch("app.agents.orchestrator.get_guard", return_value=ImmediateGuard()),
        patch(
            "app.agents.orchestrator.call_llm",
            return_value=SimpleNamespace(text='{"next_action":"revision"}'),
        ) as call_llm,
    ):
        result = orchestrator.plan(state, logger=lambda _: None)

    assert result["next_action"] == "revision"
    assert "第 4 轮会话" in call_llm.call_args.args[0]


def test_fallback_only_pauses_for_unanswered_decisions():
    base = {"current_document": "论文正文。", "user_request": "修改论文"}

    fix = orchestrator.fallback_plan({**base, "audit_findings": [{"id": 1, "kind": "fix"}]})
    decision = orchestrator.fallback_plan(
        {**base, "audit_findings": [{"id": 1, "kind": "decision"}]}
    )
    answered = orchestrator.fallback_plan(
        {
            **base,
            "audit_findings": [{"id": 1, "kind": "decision"}],
            "decisions": True,
        }
    )

    assert fix["next_action"] == "revision"
    assert decision["next_action"] == "human_approval"
    assert answered["next_action"] == "revision"
