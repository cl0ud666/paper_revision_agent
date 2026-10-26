from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents import orchestrator, revision
from app.document.preview import locate_on_pages, manual_patch_suggestion
from app.workflow.router import route_from_orchestrator

REQUEST = "全面检查并修改这篇论文，将“1.2 国内外研究现状”同一层级的所有标题设为仿宋、小六"


def test_planner_cannot_turn_comprehensive_review_into_format_only():
    state = {"user_request": REQUEST, "current_document": "## 1.2 国内外研究现状\n\n正文。\n"}
    update = orchestrator._normalize(
        {
            "next_action": "revision",
            "plan_kind": "direct_revision",
            "instructions": "仅修改标题，不要改动正文",
        },
        state,
        is_replan=False,
        log=lambda _: None,
    )
    assert update["next_action"] == "audit"
    assert update["next_instructions"] == REQUEST
    assert route_from_orchestrator({**state, "next_action": "revision"}) == "audit"
    assert (
        route_from_orchestrator({**state, "next_action": "revision", "audit_meta": {"chunks": 1}})
        == "revision"
    )
    assert (
        route_from_orchestrator({**state, "next_action": "report", "budget": {"exceeded": True}})
        == "report"
    )
    direct = {**state, "user_request": "只将二级标题改成仿宋，不用全面检查"}
    assert not orchestrator.requires_initial_audit(direct)


def test_comprehensive_content_request_is_not_removed_by_format_target():
    state = {
        "user_request": REQUEST,
        "current_document": "## 1.2 国内外研究现状\n\n原文不够准确。\n",
        "next_instructions": "仅修改标题，不改正文",
        "audit_meta": {"chunks": 1},
    }
    answer = '{"patches":[{"issue_id":1,"span_label":"P01","quote":"原文不够准确。","replace":"表述已明确适用范围。","rationale":"表达清晰"}]}'
    with patch(
        "app.agents.revision.agent.call_llm", return_value=SimpleNamespace(text=answer)
    ) as model:
        result = revision.run(state, logger=lambda _: None)
    assert model.called
    assert {p["action"] for p in result["proposed_patches"]} == {"replace_text", "set_font_format"}


def page_chars(text, page):
    return [[c, page, 0.1, 0.2, 0.11, 0.22] for c in text if not c.isspace()]


def test_toc_and_body_heading_are_disambiguated_by_real_neighboring_body_text():
    original = "# 目录\n\n1.2 国内外研究现状\t2\n\n# 1 引言\n\n独特的上一段正文。\n\n## 1.2 国内外研究现状\n\n本节介绍相关研究成果。\n"
    start = original.rindex("1.2 国内外研究现状")
    item = {
        "original_start": start,
        "original_end": start + len("1.2 国内外研究现状"),
        "action": "set_font_format",
    }
    chars = page_chars("目录1.2 国内外研究现状……2", 2) + page_chars(
        "1 引言独特的上一段正文。1.2 国内外研究现状本节介绍相关研究成果。", 5
    )
    boxes = locate_on_pages(item, original, {"chars": chars})
    assert boxes and {b["page"] for b in boxes} == {5}
    # Missing/changed context cannot be silently converted to an ordinal guess.
    bad = page_chars("1.2 国内外研究现状甲", 2) + page_chars("1.2 国内外研究现状乙", 5)
    with pytest.raises(ValueError):
        locate_on_pages(item, original, {"chars": bad})


def test_format_manual_message_is_an_operation_not_replacement_text():
    message = manual_patch_suggestion(
        {
            "action": "set_font_format",
            "before": "1.2 国内外研究现状",
            "params": {"font_name": "仿宋", "font_size_pt": 6.5},
        }
    )
    assert "人工操作" in message and "改为「设置文字格式" not in message
