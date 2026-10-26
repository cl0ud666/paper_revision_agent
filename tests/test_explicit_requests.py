import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from docx import Document

from app.agents import review, revision
from app.document.docx_model import DocxSource, w
from app.document.docx_redline import export_redline
from app.document.patches import patch_from_model, validate_patch
from app.document.requests import explicit_targets
from app.document.spans import build_spans
from app.document.structured import preflight
from app.utils.consistency import find_inconsistencies, find_naming_variants
from app.workflow.router import route_after_audit, route_from_orchestrator


def test_all_level_two_headings_are_formatted_despite_audit_findings(tmp_path):
    doc = Document()
    doc.add_heading("1 方法", 1)
    doc.add_heading("1.1 模型", 2)
    doc.add_paragraph("原文。")
    doc.add_heading("1.2 训练", 2)
    doc.add_heading("1.2.1 细节", 3)
    stream = BytesIO()
    doc.save(stream)
    data = stream.getvalue()
    path = tmp_path / "source.docx"
    path.write_bytes(data)
    state = {
        "current_document": DocxSource(data).text,
        "source_docx": str(path),
        "user_request": "把所有二级标题变为仿宋小六",
        "audit_findings": [{"id": 9, "problem": "检查原文", "span_label": "P01"}],
    }
    with patch(
        "app.agents.revision.agent.call_llm", return_value=SimpleNamespace(text='{"patches":[]}')
    ):
        result = revision.run(state, logger=lambda _: None)
    patches = [p for p in result["proposed_patches"] if p["action"] == "set_font_format"]
    assert len(patches) == 2
    assert all(p["params"] == {"font_name": "仿宋", "font_size_pt": 6.5} for p in patches)
    out = export_redline(data, [patches], str(tmp_path / "out.docx"))
    assert len(out.applied) == 2
    edited = Document(out.out_path)
    for paragraph in (edited.paragraphs[1], edited.paragraphs[3]):
        assert paragraph.runs[0].font.size.pt == 6.5
        assert paragraph.runs[0].font.name == "仿宋"
    assert edited.paragraphs[4].runs[0].font.size is None
    assert route_after_audit({**state, "audit_findings": []}) == "revision"
    assert route_from_orchestrator({**state, "next_action": "report"}) == "revision"


def test_citation_count_is_not_hidden_by_other_audit_issues():
    state = {
        "current_document": "# 1 方法\n\n采用对比学习。\n\n# 参考文献\n\n已有资料。\n",
        "user_request": "加两个文献引用",
        "audit_findings": [{"id": 4, "problem": "修改语句"}],
    }
    targets = explicit_targets(state, build_spans(state["current_document"]))
    assert targets[0]["requested_count"] == 2
    with patch(
        "app.agents.revision.agent.call_llm", return_value=SimpleNamespace(text='{"patches":[{}]}')
    ) as model:
        result = revision.run(state, logger=lambda _: None)
    assert model.called  # The unrelated prose issue still goes to Revision.
    assert any(
        "已关闭自动新增引用" in m["blocked_reason"] and "2 个" in m["suggestion"]
        for m in result["manual_items"]
    )
    assert not result["proposed_patches"]


def test_missing_letter_in_case_correction_uses_real_original():
    text = "本文对MoCO预训练阶段的超参数进行网格搜索调优。"
    spans = build_spans(text)
    patch_item = patch_from_model(
        {
            "span_label": spans[0].label,
            "quote": text.replace("MoCO", "MoC"),
            "replace": text.replace("MoCO", "MoCo"),
        },
        spans=spans,
        patch_id="fix",
    )
    validate_patch(patch_item, spans)
    assert patch_item.status == "proposed"
    assert patch_item.before == text
    assert patch_item.quote == text


def test_empty_replacement_gets_one_bounded_repair_not_a_deletion():
    issue = {
        "id": 1,
        "span_label": "P01",
        "evidence": "原文不够准确。",
        "problem": "明确适用范围",
        "suggestion": "补充本实验范围限定",
    }
    empty = {
        "patches": [{"issue_id": 1, "span_label": "P01", "quote": "原文不够准确。", "replace": ""}]
    }
    fixed = {
        "patches": [
            {
                "issue_id": 1,
                "span_label": "P01",
                "quote": "原文不够准确。",
                "replace": "本结论仅适用于当前实验设置。",
                "rationale": "限制结论范围",
            }
        ]
    }
    with patch(
        "app.agents.revision.agent.call_llm",
        side_effect=[
            SimpleNamespace(text=json.dumps(empty)),
            SimpleNamespace(text=json.dumps(fixed)),
        ],
    ) as model:
        result = revision.run(
            {"current_document": "原文不够准确。\n", "audit_findings": [issue]},
            logger=lambda _: None,
        )
    assert model.call_count == 2
    assert len(result["proposed_patches"]) == 1 and not result["manual_items"]
    assert not result["proposed_patches"][0]["delete"]


def test_already_correct_original_does_not_become_manual_work():
    text = "本文对MoCo预训练阶段的超参数进行网格搜索调优。"
    response = {
        "patches": [
            {
                "issue_id": 1,
                "span_label": "P01",
                "quote": text.replace("MoCo", "MoC"),
                "replace": text,
            }
        ]
    }
    with patch(
        "app.agents.revision.agent.call_llm",
        return_value=SimpleNamespace(text=json.dumps(response)),
    ):
        result = revision.run(
            {
                "current_document": text,
                "audit_findings": [{"id": 1, "span_label": "P01", "problem": "检查拼写"}],
            },
            logger=lambda _: None,
        )
    assert not result["manual_items"] and not result["proposed_patches"]


def test_case_scanners_ignore_code_and_inline_identifiers():
    spans = build_spans(
        "MoCO = build_model()\n\nMoCo = build_model()\n\n使用 `MoCO` 参数。\n\n使用 `MoCo` 参数。\n"
    )
    assert not find_naming_variants(spans)
    assert not find_inconsistencies(
        "\n".join(s.text for s in spans), spans=spans, mappings=[{"old": "MoCO", "new": "MoCo"}]
    )


def test_experiment_paragraphs_are_atomic_tracked_and_rejectable(tmp_path):
    doc = Document()
    doc.add_paragraph("实验设计说明。")
    data = BytesIO()
    doc.save(data)
    data = data.getvalue()
    spans = build_spans(DocxSource(data).text)
    item = patch_from_model(
        {
            "span_label": spans[0].label,
            "quote": "实验设计说明。",
            "action": "insert_paragraphs",
            "params": {
                "paragraphs": [
                    {"text": "待开展 BERT 对比实验。"},
                    {"text": "准确率 XXX；结果待填，不作有效性结论。"},
                ]
            },
            "rationale": "补充实验结构",
        },
        spans=spans,
        patch_id="experiment",
    )
    validate_patch(item, spans)
    preflight(data, item.to_json())
    out = export_redline(data, [[item.to_json()]], str(tmp_path / "out.docx"))
    assert not out.skipped and out.comments == 1
    source = DocxSource((tmp_path / "out.docx").read_bytes())
    assert "XXX" in source.text
    assert len(source.document_root.findall(f".//{w('p')}")) == 3
    assert len(source.document_root.findall(f".//{w('ins')}")) == 4
    out = export_redline(data, [[]], str(tmp_path / "excluded.docx"))
    assert "XXX" not in out.text and not out.comments


def test_summary_limitations_do_not_trigger_another_revision_or_failure_list():
    document = "本文采用 MoCo。\n"
    response = {
        "status": "fail",
        "summary": "无法从摘要核实全文",
        "remaining_issues": [{"id": 1, "problem": "修改后文本摘要中无法确认该句实际写回"}],
        "per_issue": [{"id": 1, "status": "unresolved", "reason": "全文 69 处需逐处核查"}],
        "constraint_violations": [],
    }
    with patch("app.agents.review._llm_review", return_value=response):
        result = review.run(
            {"current_document": document, "original_document": document}, logger=lambda _: None
        )
    assert result["review_result"]["status"] == "pass"
    assert not result["review_result"]["remaining_issues"]
    assert not result["review_result"]["per_issue"]


def test_old_native_citation_patches_are_blocked_at_export(tmp_path):
    doc = Document()
    doc.add_paragraph("相关研究支持本方法。")
    doc.add_heading("参考文献", 1)
    data = BytesIO()
    doc.save(data)
    data = data.getvalue()
    spans = build_spans(DocxSource(data).text)
    items = []
    for index, title in enumerate(("研究甲", "研究乙")):
        item = patch_from_model(
            {
                "span_label": spans[0].label,
                "quote": "相关研究支持本方法。",
                "action": "insert_citation",
                "params": {
                    "bib_key": f"ref{index}",
                    "bibliography_span_label": spans[-1].label,
                    "metadata": {"author": "测试作者", "title": title, "year": "2020"},
                },
                "rationale": "引用测试",
            },
            spans=spans,
            patch_id=f"cite{index}",
        )
        # Simulate an accepted patch saved by an older application version.
        item.status = "applied"
        item.before = item.quote
        items.append(item.to_json())
    result = export_redline(data, [items], str(tmp_path / "citations.docx"))
    assert not result.applied and result.comments == 2
    source = DocxSource((tmp_path / "citations.docx").read_bytes())
    fields = [n.text for n in source.document_root.iter(w("instrText"))]
    assert not fields
    assert source.text == DocxSource(data).text
    assert all("已关闭" in item["reason"] for item in result.skipped)


def test_numbered_heading_example_matches_peer_level_and_preserves_runs(tmp_path):
    from docx.enum.style import WD_STYLE_TYPE

    doc = Document()
    doc.add_heading("1 引言", 1)
    doc.styles.add_style("标题 2", WD_STYLE_TYPE.PARAGRAPH)
    doc.add_paragraph("1.1 研究背景", "标题 2")
    para = doc.add_paragraph()
    para.add_run("1.2\t")
    para.add_run("国内外研究现状")
    # A misleading template style must not turn 2.2.1 into a level-two heading.
    doc.add_heading("2.2.1 具体实现", 2)
    doc.add_paragraph("1.2 节介绍国内外研究现状。")
    doc.add_heading("2 理论基础", 1)
    doc.add_paragraph("2.1 基础方法")
    stream = BytesIO()
    doc.save(stream)
    data = stream.getvalue()
    state = {
        "current_document": DocxSource(data).text,
        "user_request": "把1.2 国内外研究现状这个层级的字体改为仿宋_GB2312，字号小六（7.5pt）",
    }
    with patch(
        "app.agents.revision.agent.call_llm", side_effect=AssertionError("格式请求不需要模型")
    ):
        result = revision.run(state, logger=lambda _: None)
    assert len(result["proposed_patches"]) == 3 and not result["manual_items"]
    assert route_from_orchestrator({**state, "next_action": "report"}) == "revision"
    out = export_redline(data, [result["proposed_patches"]], str(tmp_path / "headings.docx"))
    assert len(out.applied) == 3 and out.comments == 3 and not out.skipped
    edited = Document(out.out_path)
    for index in (1, 2, 6):
        for run in edited.paragraphs[index].runs:
            if not run.text:
                continue  # The comment reference is not original heading text.
            assert run.font.size.pt == 7.5
            assert (
                run._element.find(f"{w('rPr')}/{w('rFonts')}").get(w("eastAsia")) == "仿宋_GB2312"
            )
            assert run._element.find(f"{w('rPr')}/{w('rPrChange')}") is not None
    assert len([r for r in edited.paragraphs[2].runs if r.text]) == 2
    assert edited.paragraphs[2].text == "1.2\t国内外研究现状"
    for index in (0, 3, 4, 5):
        assert edited.paragraphs[index].runs[0].font.size is None
    assert (
        DocxSource(data).parts["word/styles.xml"]
        == DocxSource((tmp_path / "headings.docx").read_bytes()).parts["word/styles.xml"]
    )


def test_combined_format_table_citation_request_does_not_drop_formatting():
    state = {
        "current_document": "## 1.2 国内外研究现状\n\n研究内容。\n",
        "user_request": "把1.2 国内外研究现状这个层级改成仿宋小六，加一个表格，加两个文献引用",
    }
    with patch("app.agents.revision.agent.call_llm", side_effect=AssertionError("不调用模型")):
        result = revision.run(state, logger=lambda _: None)
    assert len(result["proposed_patches"]) == 1
    assert result["proposed_patches"][0]["action"] == "set_font_format"
    assert len(result["manual_items"]) == 2
    assert all("已关闭" in item["blocked_reason"] for item in result["manual_items"])


def test_specific_heading_does_not_change_other_headings_and_missing_target_is_reported():
    document = "## 1.1 背景\n\n## 1.2 国内外研究现状\n\n## 2.1 方法\n"
    with patch("app.agents.revision.agent.call_llm", side_effect=AssertionError("不调用模型")):
        result = revision.run(
            {"current_document": document, "user_request": "把1.2 国内外研究现状改为仿宋小六"},
            logger=lambda _: None,
        )
        missing = revision.run(
            {"current_document": document, "user_request": "把3.2 实验分析改为仿宋小六"},
            logger=lambda _: None,
        )
    assert len(result["proposed_patches"]) == 1
    assert result["proposed_patches"][0]["quote"] == "1.2 国内外研究现状"
    assert not missing["proposed_patches"]
    assert "未在正文中可靠定位" in missing["manual_items"][0]["blocked_reason"]
