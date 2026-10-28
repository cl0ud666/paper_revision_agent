"""Validated edits against existing Word objects; disabled additions remain manual."""

import copy
from hashlib import sha256

from lxml import etree

from app.document.docx_model import w
from app.document.operations.formulas import formula_xml as formula_xml
from app.document.operations.tables import _table
from app.document.operations.validation import (
    describe_operation as describe_operation,
    is_structured as is_structured,
    signature as signature,
    validate_operation as validate_operation,
)
from app.document.operations.xml import (
    M as M,
    _font_runs,
    _range_runs,
    _run,
    _set,
    _style,
    _text,
    _tracked_properties,
    _unsafe,
)
from app.utils.text import looks_like_code


def apply_operation(writer, item, block):
    """Apply on a detached block/parts snapshot; any exception rolls back parts.

    The original block element keeps its identity so other original anchors
    remain usable. A changed table is fingerprint-checked by the caller.
    """
    p, action = item.get("params") or {}, item.get("action")
    validate_operation(action, p)
    if block is None:
        raise ValueError("找不到结构修改的原始锚点")
    element = block.element
    clone = copy.deepcopy(element)
    if looks_like_code(block.text):
        raise ValueError("代码块禁止自动修改")
    old_parts, next_id = dict(writer.source.parts), writer._next_id
    appended_paragraphs = []
    try:
        if action == "table_edit":
            _table(writer, clone, p)
        elif action == "set_font_format":
            if clone.tag != w("p"):
                raise ValueError("字体补丁必须精确指向正文段落，表格请使用单元格操作")
            runs = _font_runs(clone, item.get("before") or item.get("quote"))

            def update(props):
                if "font_name" in p:
                    _set(
                        props,
                        "rFonts",
                        ascii=p["font_name"],
                        hAnsi=p["font_name"],
                        eastAsia=p["font_name"],
                        cs=p["font_name"],
                    )
                    fonts = props.find(w("rFonts"))
                    for key in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
                        fonts.attrib.pop(w(key), None)
                if "font_size_pt" in p:
                    for tag in ("sz", "szCs"):
                        _set(props, tag, val=int(p["font_size_pt"] * 2))
                for key, tags in (("bold", ("b", "bCs")), ("italic", ("i", "iCs"))):
                    if key in p:
                        for tag in tags:
                            _set(props, tag, val=int(p[key]))

            for run in runs:
                _tracked_properties(writer, run, "rPr", update)
        elif action == "set_paragraph_format":
            if clone.tag != w("p") or _unsafe(clone):
                raise ValueError("该段含特殊对象，不能自动改变段落版式")
            if p.get("style_id"):
                _style(writer.source, p["style_id"], "paragraph")

            def update(props):
                if p.get("style_id"):
                    _set(props, "pStyle", val=p["style_id"])
                if p.get("alignment"):
                    _set(props, "jc", val=p["alignment"])
                for key, attr in (
                    ("space_before_pt", "before"),
                    ("space_after_pt", "after"),
                    ("line_spacing", "line"),
                ):
                    if key in p:
                        _set(
                            props,
                            "spacing",
                            **{attr: round(p[key] * (240 if attr == "line" else 20))},
                        )
                if "line_spacing" in p:
                    _set(props, "spacing", lineRule="auto")

            _tracked_properties(writer, clone, "pPr", update)
        elif action == "insert_paragraphs":
            if clone.tag != w("p"):
                raise ValueError("新增段落须定位到正文段落或标题")
            quote = item.get("before") or item.get("quote") or ""
            if not quote or _text(clone).count(quote) != 1:
                raise ValueError("新增段落的原文锚点不唯一")
            for row in p["paragraphs"]:
                paragraph = etree.Element(w("p"))
                props = etree.SubElement(paragraph, w("pPr"))
                if row.get("style_id"):
                    _style(writer.source, row["style_id"], "paragraph")
                    _set(props, "pStyle", val=row["style_id"])
                # Track both text and the new paragraph mark.
                writer._stamp(etree.SubElement(etree.SubElement(props, w("rPr")), w("ins")))
                insertion = writer._stamp(etree.SubElement(paragraph, w("ins")))
                insertion.append(_run(row["text"]))
                appended_paragraphs.append(paragraph)
        elif action == "insert_formula":
            if clone.tag != w("p"):
                raise ValueError("公式需定位到正文段落；表格公式暂不自动插入")
            runs = _range_runs(clone, item.get("before") or item.get("quote"))
            formula = formula_xml(p["latex"])
            insertion = writer._stamp(etree.Element(w("ins")))
            insertion.append(formula)
            if p.get("position", "after") == "after":
                runs[-1].addnext(insertion)
            else:
                runs[0].addprevious(insertion)
        # Commit only after every check and conversion succeeded.
        element.attrib.clear()
        element.attrib.update(clone.attrib)
        element.text = clone.text
        for child in list(element):
            element.remove(child)
        for child in list(clone):
            element.append(child)
        cursor = element
        for paragraph in appended_paragraphs:
            cursor.addnext(paragraph)
            cursor = paragraph
    except Exception:
        writer.source.parts = old_parts
        writer._next_id = next_id
        raise
    anchor = element.find(f".//{w('p')}") if element.tag == w("tbl") else element
    writer.note_on_paragraph(
        anchor,
        f"原文：{item.get('before', '')}\n修改为：{describe_operation(action, p)}\n原因：{item.get('rationale', '')}",
    )
    writer.changes += 1


def block_for(source, item):
    from app.document.spans import build_spans, resolve_span

    span = resolve_span(
        build_spans(source.text), span_id=item.get("span_id", ""), label=item.get("span_label", "")
    )
    if span is None:
        raise ValueError("结构修改的段落无法定位")
    block = source.block_at(span.start)
    if block is None:
        raise ValueError("结构修改的Word块无法定位")
    return block


def preflight(source_bytes, item):
    from app.document.docx_model import DocxSource
    from app.document.docx_redline import RedlineWriter

    source = DocxSource(source_bytes)
    block = block_for(source, item)
    apply_operation(RedlineWriter(source, comments=False), item, block)
    return sha256(etree.tostring(block.element)).hexdigest()


def document_capabilities(path):
    """Small factual inventory for the model, never infer formatting from text."""
    from pathlib import Path

    from app.document.docx_model import DocxSource
    from app.document.spans import build_spans

    source = DocxSource(Path(path).read_bytes())
    result = []
    for span in build_spans(source.text):
        block = source.block_at(span.start)
        if block is None:
            continue
        entry = {"span_label": span.label, "kind": block.kind}
        if block.kind == "table":
            entry["rows"] = [
                [_text(cell) for cell in row.findall(w("tc"))]
                for row in block.element.findall(w("tr"))
            ]
            entry["has_merged_cells"] = (
                block.element.find(f".//{w('gridSpan')}") is not None
                or block.element.find(f".//{w('vMerge')}") is not None
            )
        else:
            entry["style"] = source._paragraph_style(block.element)
            style_node = block.element.find(f"{w('pPr')}/{w('pStyle')}")
            entry["style_id"] = style_node.get(w("val")) if style_node is not None else ""
            entry["direct_runs"] = []
            for run in block.element.findall(w("r")):
                if not _text(run):
                    continue
                props = run.find(w("rPr"))
                info = {"text": _text(run)}
                if props is not None:
                    for key, tag, attr in (
                        ("font_name", "rFonts", "eastAsia"),
                        ("font_size_half_points", "sz", "val"),
                        ("bold", "b", "val"),
                    ):
                        node = props.find(w(tag))
                        if node is not None:
                            info[key] = node.get(w(attr), "true" if key == "bold" else "")
                entry["direct_runs"].append(info)
        result.append(entry)
    return result
