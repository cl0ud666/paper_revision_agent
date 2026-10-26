"""Global conventions must be routed to evidence, not the cover page."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from app.agents import revision
from app.agents.revision import targets as revision_targets
from app.document.spans import build_spans
from app.utils.context import _b_approved_issues


def term_issue():
    return {
        "id": 9,
        "span_label": "P01",
        "kind": "fix",
        "evidence": "设计了两阶段架构。",
        "problem": "全文 batch、batch size、epoch 不能混用，统一中文",
        "suggestion": "首次出现写批大小（Batch Size）、训练轮次（Epoch），后续统一中文",
    }


def test_no_terms_anywhere_is_not_manual_work():
    with patch("app.agents.revision.agent.call_llm", side_effect=AssertionError("no model needed")):
        update = revision.run(
            {"current_document": "设计了两阶段架构。\n", "audit_findings": [term_issue()]},
            logger=lambda _: None,
        )
    assert not update["manual_items"]
    assert not update["proposed_patches"]


def test_global_terms_go_to_all_relevant_chunks_with_global_first_occurrence():
    spans = build_spans(
        "设计了两阶段架构。\n\n"
        + "研究背景。" * 2000
        + "\n\n设置 batch size 为32。\n\n"
        + "实验设置。" * 2000
        + "\n\n训练 epoch 为100。\n"
    )
    targets = revision_targets._scope_targets(spans, [term_issue()])
    groups = revision_targets._group_targets(spans, targets, 600)
    assert len(groups) == 2
    assert all(any("batch" in s.text or "epoch" in s.text for s in chunk) for chunk, _ in groups)
    assert all(
        "first_occurrence_labels" in _b_approved_issues({}, {"issues": issues})
        for _, issues in groups
    )
    assert targets[0]["evidence"] == "设置 batch size 为32。"


def test_unlocated_general_request_is_not_limited_to_first_chunk():
    spans = build_spans("第一部分。" * 1500 + "\n\n" + "第二部分。" * 1500)
    groups = revision_targets._group_targets(
        spans, [{"id": 1, "problem": "全面检查", "source": "user_request"}], 600
    )
    assert len(groups) > 1


def test_absent_target_spelling_does_not_discard_unknown_source_alias():
    spans = build_spans("正文使用动量对比学习。\n")
    issue = {"id": 2, "problem": "全文统一为 MoCo", "suggestion": "全文统一为 MoCo"}
    assert revision_targets._scope_targets(spans, [issue]) == [issue]


def test_absence_in_one_chunk_does_not_pollute_successful_issue():
    response = SimpleNamespace(
        text='{"patches":[{"issue_id":9,"span_label":"P01","quote":"batch size","replace":"批大小（Batch Size）","rationale":"按确认口径统一"}],"unfulfilled":[{"issue_id":9,"reason":"本块并未出现其他术语，无可修改之处"}]}'
    )
    with patch("app.agents.revision.agent.call_llm", return_value=response):
        update = revision.run(
            {"current_document": "设置 batch size 为32。\n", "audit_findings": [term_issue()]},
            logger=lambda _: None,
        )
    assert len(update["proposed_patches"]) == 1
    assert not update["manual_items"]


def test_real_blocked_term_has_exact_evidence():
    response = SimpleNamespace(
        text='{"patches":[],"unfulfilled":[{"issue_id":9,"span_label":"P01","quote":"epoch 与批次的关系待定","reason":"缺少实际训练配置，不能判断原文所指单位","suggestion":"请核对配置"}]}'
    )
    with patch("app.agents.revision.agent.call_llm", return_value=response):
        update = revision.run(
            {"current_document": "epoch 与批次的关系待定。\n", "audit_findings": [term_issue()]},
            logger=lambda _: None,
        )
    assert len(update["manual_items"]) == 1
    assert update["manual_items"][0]["evidence"] == "epoch 与批次的关系待定"


def test_category_count_consistency_is_not_a_global_terminology_request():
    issue = {
        "problem": "摘要只说五类攻击及正常行为，正文采用6-way 5-shot，需统一口径",
        "suggestion": "在摘要明确写为六类（五类攻击及正常行为）或6-way",
    }
    assert not revision_targets._scope_terms(issue)


def test_cross_section_context_includes_abstract_even_without_other_labels():
    text = (
        "# 摘 要\n\n对五类攻击及正常行为分类。\n\n# 1 引言\n\n"
        + "背景。" * 2000
        + "\n\n# 6 总结\n\n构建6-way分类。\n"
    )
    spans = build_spans(text)
    issue = {
        "id": 15,
        "span_label": spans[-1].label,
        "problem": f"摘要 {spans[1].label} 与结论 {spans[-1].label} 类别数需统一口径",
        "suggestion": "在摘要明确总数为六类。",
    }
    groups = revision_targets._group_targets(
        spans, revision_targets._scope_targets(spans, [issue]), 600
    )
    assert len(groups) == 1
    assert spans[1] in groups[0][0]
    assert spans[-1] in groups[0][0]


def test_missing_context_retries_against_full_document_not_manual_todo():
    text = "# 摘要\n\n对五类攻击及正常行为分类。\n\n# 6 总结\n\n构建6-way分类。\n"
    spans = build_spans(text)
    issue = {
        "id": 15,
        "span_label": spans[-1].label,
        "problem": "摘要与结论类别数不一致",
        "suggestion": "摘要明确类别总数为六类",
    }
    missing = {
        "patches": [],
        "unfulfilled": [{"issue_id": 15, "reason": "摘要不在本片段内，无法在本轮定位并修改"}],
    }
    success = {
        "patches": [
            {
                "issue_id": 15,
                "span_label": spans[1].label,
                "quote": "五类攻击及正常行为",
                "replace": "六类（五类攻击及正常行为）",
                "rationale": "明确类别总数",
            }
        ]
    }
    with patch(
        "app.agents.revision.agent.call_llm",
        side_effect=[
            SimpleNamespace(text=json.dumps(missing)),
            SimpleNamespace(text=json.dumps(success)),
        ],
    ) as model:
        update = revision.run(
            {"current_document": text, "audit_findings": [issue]}, logger=lambda _: None
        )
    assert model.call_count == 2
    assert "对五类攻击及正常行为分类。" in model.call_args.args[0]
    assert not update["manual_items"]
    assert len(update["proposed_patches"]) == 1


def test_missing_context_retry_is_bounded_and_honest_when_still_unresolved():
    issue = {
        "id": 15,
        "span_label": "P01",
        "problem": "摘要需要完善",
        "suggestion": "摘要明确类别数",
    }
    missing = {
        "patches": [],
        "unfulfilled": [{"issue_id": 15, "status": "missing_context", "reason": "摘要不在本片段"}],
    }
    with patch(
        "app.agents.revision.agent.call_llm", return_value=SimpleNamespace(text=json.dumps(missing))
    ) as model:
        update = revision.run(
            {"current_document": "摘要内容。\n", "audit_findings": [issue]}, logger=lambda _: None
        )
    assert model.call_count == 2
    assert len(update["manual_items"]) == 1
    assert "不在本片段" not in update["manual_items"][0]["blocked_reason"]
    assert "重试" in update["manual_items"][0]["blocked_reason"]


def test_explicit_not_applicable_does_not_become_fallback_manual():
    response = SimpleNamespace(
        text='{"patches":[],"unfulfilled":[{"issue_id":1,"status":"not_applicable","reason":"原文已符合口径"}]}'
    )
    with patch("app.agents.revision.agent.call_llm", return_value=response):
        update = revision.run(
            {
                "current_document": "使用MoCo。\n",
                "audit_findings": [{"id": 1, "problem": "检查MoCo写法"}],
            },
            logger=lambda _: None,
        )
    assert not update["manual_items"]


def test_revision_context_never_cuts_target_text_or_selected_choice():
    from app.utils.context import build_context

    target = "目标摘要中间内容。" * 200
    fields = build_context(
        {"current_document": target},
        "revision",
        budget_tokens=150,
        extra={
            "chunk_text": target,
            "issues": [{"id": 1, "selected_option": "保留原文并补齐数据"}],
            "round_patches": [{"quote": "无关历史" * 500}],
        },
    )
    assert fields["chunk_text"] == target
    assert "保留原文并补齐数据" in fields["approved_issues"]
