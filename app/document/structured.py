"""Typed, reviewable OOXML edits. No arbitrary XML from the model is accepted.

All coordinates address the immutable original. Each operation is validated on
a detached element; only a completely prepared edit is committed. Parts not
participating in an edit (images, macros, headers, etc.) remain byte-identical.
"""

from __future__ import annotations

import copy
import json
import re
from hashlib import sha256
from uuid import uuid4

from lxml import etree

from app.document.docx_model import W_NS, w
from app.utils.text import looks_like_code

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
B = "http://schemas.openxmlformats.org/officeDocument/2006/bibliography"
DS = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
ACTIONS = {
    "set_font_format",
    "set_paragraph_format",
    "table_edit",
    "insert_formula",
    "insert_citation",
    "add_bibliography_item",
    "insert_paragraphs",
}
TABLE_OPS = {
    "set_cell_text",
    "add_row",
    "delete_row",
    "add_column",
    "delete_column",
    "merge_cells",
    "split_cell",
    "set_table_format",
}


def is_structured(patch):
    return patch.get("action", "replace_text") != "replace_text"


def signature(patch):
    return json.dumps(
        [patch.get("action", "replace_text"), patch.get("params", {})],
        ensure_ascii=False,
        sort_keys=True,
    )


def _int(value, name, low=0, high=1000):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} 必须是 {low}–{high} 的整数")
    return value


def validate_operation(action, p):
    from app.document.operation_policy import disabled_operation_reason

    disabled = disabled_operation_reason(action, p)
    if disabled:
        raise ValueError(disabled)
    if action not in ACTIONS or not isinstance(p, dict):
        raise ValueError("不支持的结构操作，不能用文字替换冒充执行")
    if len(json.dumps(p, ensure_ascii=False)) > 16000:
        raise ValueError("单条结构修改过大，请拆分成可核对的补丁")
    if action == "set_font_format":
        if not any(k in p for k in ("font_size_pt", "font_name", "bold", "italic")):
            raise ValueError("缺少具体字体、字号或粗体设置")
        size = p.get("font_size_pt", 12)
        if (
            isinstance(size, bool)
            or not isinstance(size, (int, float))
            or not 5 <= size <= 72
            or size * 2 != int(size * 2)
        ):
            raise ValueError("字号必须为 5–72 磅，步长为半磅")
        for key in ("bold", "italic"):
            if key in p and not isinstance(p[key], bool):
                raise ValueError(f"{key} 必须为布尔值")
        if "font_name" in p and (
            not isinstance(p["font_name"], str) or not 1 <= len(p["font_name"]) <= 80
        ):
            raise ValueError("字体名称不合法")
    elif action == "set_paragraph_format":
        if not any(
            k in p
            for k in ("style_id", "alignment", "space_before_pt", "space_after_pt", "line_spacing")
        ):
            raise ValueError("缺少具体段落格式设置")
        if "alignment" in p and p["alignment"] not in {"left", "center", "right", "both"}:
            raise ValueError("不支持的对齐方式")
        for key in ("space_before_pt", "space_after_pt", "line_spacing"):
            if key in p and (
                isinstance(p[key], bool)
                or not isinstance(p[key], (float, int))
                or not 0 <= p[key] <= (5 if key == "line_spacing" else 100)
            ):
                raise ValueError("段落间距超出安全范围")
    elif action == "table_edit":
        op = p.get("operation")
        if op not in TABLE_OPS:
            raise ValueError("不支持的表格操作")
        for key in ("row", "column", "end_column"):
            if key in p:
                _int(p[key], key)
        if op == "set_cell_text" and not isinstance(p.get("text"), str):
            raise ValueError("必须提供单元格修改后文本")
        if op in {"add_row", "add_column"} and (
            not isinstance(p.get("values"), list)
            or not all(isinstance(v, str) for v in p["values"])
        ):
            raise ValueError("新增行/列必须提供各单元格内容，不能猜测实验数据")
        if op == "set_table_format":
            if not any(
                k in p for k in ("style_id", "border_color", "shading", "column_widths_twips")
            ):
                raise ValueError("缺少表格格式参数")
            for key in ("border_color", "shading"):
                if key in p and not re.fullmatch(r"[0-9a-fA-F]{6}", str(p[key])):
                    raise ValueError("颜色必须是六位 RGB 十六进制值")
            if "column_widths_twips" in p:
                if not isinstance(p["column_widths_twips"], list):
                    raise ValueError("列宽必须是数组")
                for value in p["column_widths_twips"]:
                    _int(value, "列宽 twips", 100, 20000)
    elif action == "insert_paragraphs":
        paragraphs = p.get("paragraphs")
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 20:
            raise ValueError("新增结构需包含 1–20 个具体段落")
        for paragraph in paragraphs:
            if (
                not isinstance(paragraph, dict)
                or not isinstance(paragraph.get("text"), str)
                or not paragraph["text"].strip()
            ):
                raise ValueError("新增段落必须提供具体内容")
            if len(paragraph["text"]) > 3000:
                raise ValueError("单段新增内容过长，请拆分")
    elif action == "insert_formula":
        if not isinstance(p.get("latex"), str) or not 1 <= len(p["latex"]) <= 2000:
            raise ValueError("需要不超过 2000 字的 LaTeX 公式")
        if p.get("position", "after") not in {"before", "after"}:
            raise ValueError("公式仅支持在原文锚点前后插入，不覆盖已有公式/图片")
    else:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(p.get("bib_key", ""))):
            raise ValueError("文献标识必须为字母开头的安全标识")
        meta = p.get("metadata")
        if not isinstance(meta, dict) or not all(
            isinstance(meta.get(k), str) and meta[k].strip() for k in ("author", "title", "year")
        ):
            raise ValueError("缺少真实文献的作者、标题或年份，不能编造引用")
        if not re.fullmatch(r"\d{4}", meta["year"]):
            raise ValueError("文献年份应为四位数字")
        # New model candidates may omit evidence: the Revision gateway then
        # independently resolves their metadata before admitting the patch.
        if action == "insert_citation" and not p.get("bibliography_span_label"):
            raise ValueError("需明确参考文献列表插入位置，正文引用与文末条目必须一起确认")


def verify_source_evidence(item, document, request=""):
    """Model-supplied 'verified=true' is never evidence."""
    if item.get("action") not in {"insert_citation", "add_bibliography_item"}:
        return
    p = item.get("params") or {}
    evidence = str(p.get("source_evidence") or "").strip()
    if not evidence or evidence not in document and evidence not in request:
        raise ValueError("文献信息不在原文或用户提供的材料中，需作者提供可核验来源")
    meta = p.get("metadata") or {}
    normalized = re.sub(r"\s+", "", evidence).casefold()
    for key in ("author", "title", "year", "journal", "doi", "pages"):
        if meta.get(key) and re.sub(r"\s+", "", str(meta[key])).casefold() not in normalized:
            raise ValueError(f"文献 {key} 与所提供来源不一致，需人工核验")


def describe_operation(action, p):
    if action == "set_font_format":
        names = {
            "font_name": "字体",
            "font_size_pt": "字号（磅）",
            "bold": "粗体",
            "italic": "斜体",
        }
        return "设置文字格式：" + "；".join(f"{names[k]}={v}" for k, v in p.items() if k in names)
    if action == "set_paragraph_format":
        names = {
            "style_id": "段落样式",
            "alignment": "对齐方式",
            "space_before_pt": "段前（磅）",
            "space_after_pt": "段后（磅）",
            "line_spacing": "行距倍数",
        }
        return "设置段落格式：" + "；".join(f"{names[k]}={v}" for k, v in p.items() if k in names)
    if action == "table_edit":
        names = {
            "set_cell_text": "修改单元格",
            "add_row": "新增行",
            "delete_row": "删除行",
            "add_column": "新增列",
            "delete_column": "删除列",
            "merge_cells": "合并单元格",
            "split_cell": "拆分单元格",
            "set_table_format": "设置表格格式",
        }
        detail = {k: v for k, v in p.items() if k != "operation"}
        for k in ("row", "column", "end_column"):
            if k in detail:
                detail[k] += 1
        return (
            names.get(p.get("operation"), "修改表格")
            + "（行列从 1 开始） "
            + json.dumps(detail, ensure_ascii=False)
        )
    if action == "insert_formula":
        return "插入可编辑公式：" + p.get("latex", "")
    if action == "insert_paragraphs":
        return "在原文段落后新增：\n" + "\n".join(row["text"] for row in p["paragraphs"])
    meta = p.get("metadata") or {}
    return (
        "插入引用并添加文献：" if action == "insert_citation" else "添加参考文献："
    ) + reference_text(meta)


def reference_text(meta):
    return ". ".join(
        str(meta[k]).strip()
        for k in ("author", "title", "journal", "year", "pages", "doi")
        if meta.get(k)
    )


def _text(element):
    return "".join(
        n.text or ""
        for n in element.iter(w("t"))
        if not any(a.tag == w("del") for a in n.iterancestors())
    )


def _unsafe(element):
    return any(
        n.tag
        in {
            w("drawing"),
            w("pict"),
            w("object"),
            w("fldChar"),
            w("fldSimple"),
            w("instrText"),
            w("sdt"),
            f"{{{M}}}oMath",
            f"{{{M}}}oMathPara",
        }
        for n in element.iter()
    )


def _prop(element, tag):
    node = element.find(w(tag))
    if node is None:
        node = etree.Element(w(tag))
        element.insert(0, node)
    return node


def _set(parent, tag, **attrs):
    node = parent.find(w(tag))
    if node is None:
        node = etree.SubElement(parent, w(tag))
    for key, value in attrs.items():
        node.set(w(key), str(value))
    return node


def _run(text):
    run = etree.Element(w("r"))
    node = etree.SubElement(run, w("t"))
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text
    return run


def _tracked_properties(writer, element, tag, update):
    props = _prop(element, tag)
    if props.find(w(tag + "Change")) is not None:
        raise ValueError("该位置已有未接受的格式修订，需先核对原有修订")
    previous = copy.deepcopy(props)
    update(props)
    change = writer._stamp(etree.SubElement(props, w(tag + "Change")))
    change.append(previous)


def formula_xml(latex):
    # A parser, not a TeX execution engine. File IO/macros/URLs are disallowed.
    if re.search(
        r"\\(?:input|include|write|read|def|newcommand|href|url|html|begin\s*\{document)", latex
    ):
        raise ValueError("公式包含不允许的命令")
    try:
        from latex2mathml.converter import convert as to_mathml
        from mathml2omml import convert as to_omml
    except ImportError as exc:
        raise ValueError("公式转换组件尚未安装，需更新部署依赖") from exc
    try:
        result = to_omml(to_mathml(latex))
        # mathml2omml 0.0.2 closes groupChrPr as groupChr in MOver/MUnder.
        # Correct ONLY those two known serializer tokens, then strictly parse;
        # never recover arbitrary malformed XML or silently discard formulae.
        for position in ("top", "bot"):
            result = result.replace(
                f'<m:pos m:val="{position}"/></m:groupChr>',
                f'<m:pos m:val="{position}"/></m:groupChrPr>',
            )
        root = etree.fromstring(
            f'<root xmlns:m="{M}" xmlns:w="{W_NS}">{result}</root>'.encode(),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )
        math = root.find(f"{{{M}}}oMath")
        if math is None or not len(math):
            raise ValueError("转换结果为空")
        # Unknown commands must not silently survive as literal LaTeX.
        if "\\" in "".join(math.itertext()):
            raise ValueError("公式包含转换器未识别的命令")
        return math
    except Exception as exc:
        raise ValueError(f"公式无法可靠转为原生 Word 公式：{exc}") from exc


def _style(source, style_id, kind):
    raw = source.parts.get("word/styles.xml")
    if not raw or not any(
        n.get(w("styleId")) == style_id and n.get(w("type")) == kind
        for n in etree.fromstring(raw).iter(w("style"))
    ):
        raise ValueError("目标样式不在原文样式表中，不能猜测或覆盖全局样式")


def _range_runs(paragraph, quote):
    """Split only plain text runs; never flatten a hyperlink/field/math node."""
    if not quote:
        raise ValueError("格式修改缺少明确的原文范围")
    text = _text(paragraph)
    if text.count(quote) != 1:
        raise ValueError("结构修改的原文锚点已改变或不唯一")
    start, end = text.index(quote), text.index(quote) + len(quote)
    runs, cursor = [], 0
    for run in list(paragraph):
        part = _text(run)
        lo, hi = cursor, cursor + len(part)
        cursor = hi
        if max(lo, start) >= min(hi, end):
            continue
        if run.tag != w("r") or any(n.tag not in {w("rPr"), w("t")} for n in run):
            raise ValueError("目标范围包含超链接、已有修订、公式或特殊对象，需人工核对")
        selected = None
        for a, b in ((lo, min(hi, start)), (max(lo, start), min(hi, end)), (max(lo, end), hi)):
            if a >= b:
                continue
            piece = copy.deepcopy(run)
            for n in list(piece):
                if n.tag != w("rPr"):
                    piece.remove(n)
            t = etree.SubElement(piece, w("t"))
            t.text = part[a - lo : b - lo]
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            run.addprevious(piece)
            if start <= a and b <= end:
                selected = piece
        paragraph.remove(run)
        if selected is not None:
            runs.append(selected)
    if not runs:
        raise ValueError("未找到普通文字 run")
    return runs


def _font_runs(paragraph, quote):
    """Whole-title formatting preserves tabs, numbering and existing run boundaries."""
    from app.document.docx_model import paragraph_text

    if _unsafe(paragraph):
        raise ValueError("标题包含公式、图片或域，需人工修改格式；原有对象保持不变")
    if any(n.tag in {w("ins"), w("del"), w("moveFrom"), w("moveTo")} for n in paragraph.iter()):
        raise ValueError("标题包含尚未处理的修订，需人工确认后修改格式")
    if (
        quote
        and re.sub(r"\s+", " ", paragraph_text(paragraph)).strip()
        == re.sub(r"\s+", " ", quote).strip()
    ):
        runs = [run for run in paragraph.iter(w("r")) if any(n.tag == w("t") for n in run)]
        if runs:
            return runs
    return _range_runs(paragraph, quote)


def _replace_cell(writer, cell, expected, text):
    from app.document.patches import changes_code

    if isinstance(expected, str) and changes_code(expected, text, _text(cell)):
        raise ValueError("单元格中的代码或变量名受到保护，不能自动修改")
    if _unsafe(cell) or cell.find(f".//{w('tbl')}") is not None or looks_like_code(_text(cell)):
        raise ValueError("单元格含公式、图片、域、嵌套表格或代码，不能覆盖")
    paras = cell.findall(w("p"))
    if len(paras) != 1 or _text(paras[0]) != expected:
        raise ValueError("单元格原文不匹配或含多个段落，需精确文字补丁")
    para = paras[0]
    if any(n.tag not in {w("pPr"), w("r")} for n in para):
        raise ValueError("单元格已有修订或批注，不能覆盖")
    deleted = writer._stamp(etree.Element(w("del")))
    template = para.find(w("r"))
    for child in list(para):
        if child.tag != w("pPr"):
            para.remove(child)
            for t in child.iter(w("t")):
                t.tag = w("delText")
            deleted.append(child)
    if len(deleted):
        para.append(deleted)
    if text:
        insertion = writer._stamp(etree.SubElement(para, w("ins")))
        run = _run(text)
        props = template.find(w("rPr")) if template is not None else None
        if props is not None:
            run.insert(0, copy.deepcopy(props))
        insertion.append(run)


def _table(writer, table, p):
    if table.tag != w("tbl"):
        raise ValueError("表格操作必须指向表格块")
    rows = table.findall(w("tr"))
    grid = table.find(w("tblGrid"))
    if not rows or grid is None:
        raise ValueError("表格缺少有效网格")
    op = p["operation"]
    if op == "set_cell_text":
        r, c = _int(p.get("row"), "row"), _int(p.get("column"), "column")
        if r >= len(rows) or c >= len(rows[r].findall(w("tc"))):
            raise ValueError("单元格坐标越界")
        _replace_cell(writer, rows[r].findall(w("tc"))[c], p.get("expected_text"), p["text"])
        return
    if any(
        n.tag in {w("vMerge"), w("hMerge"), w("gridBefore"), w("gridAfter")} for n in table.iter()
    ):
        raise ValueError("表格有纵向/旧式合并或不规则网格，结构改动需要人工处理")
    from app.document.patches import changes_code

    if _unsafe(table) or any(
        changes_code(_text(cell), "", _text(cell)) for cell in table.iter(w("tc"))
    ):
        raise ValueError("表格包含公式、图片、域或代码，结构改动需要人工处理")
    if any(
        n.tag in {w("ins"), w("del"), w("cellIns"), w("cellDel"), w("commentRangeStart")}
        for n in table.iter()
    ):
        raise ValueError("表格已有修订/批注，不能叠加网格结构修改")
    cells = [row.findall(w("tc")) for row in rows]
    cols = len(grid.findall(w("gridCol")))
    if op not in {"split_cell", "set_table_format"} and (
        any(len(row) != cols for row in cells) or table.find(f".//{w('gridSpan')}") is not None
    ):
        raise ValueError("仅规则且未合并的表格支持该结构操作")
    if op == "set_table_format":
        if p.get("style_id"):
            _style(writer.source, p["style_id"], "table")
        widths = p.get("column_widths_twips")
        if widths and (len(widths) != cols or sum(widths) > 20000):
            raise ValueError("列宽数量与网格不符或总宽度过大")

        def update(props):
            if p.get("style_id"):
                _set(props, "tblStyle", val=p["style_id"])
            if p.get("border_color"):
                borders = _prop(props, "tblBorders")
                for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                    _set(borders, side, val="single", sz=4, color=p["border_color"])
            if p.get("shading"):
                _set(props, "shd", val="clear", fill=p["shading"])

        _tracked_properties(writer, table, "tblPr", update)
        if widths:
            previous = copy.deepcopy(grid)
            for col, width in zip(grid.findall(w("gridCol")), widths):
                col.set(w("w"), str(width))
            change = writer._stamp(etree.SubElement(grid, w("tblGridChange")))
            change.append(previous)
            for row in cells:
                cursor = 0
                for cell in row:
                    span = cell.find(f"{w('tcPr')}/{w('gridSpan')}")
                    count = int(span.get(w("val"))) if span is not None else 1
                    width = sum(widths[cursor : cursor + count])
                    cursor += count
                    _tracked_properties(
                        writer,
                        cell,
                        "tcPr",
                        lambda props, size=width: _set(props, "tcW", w=size, type="dxa"),
                    )
        return
    if op in {"add_row", "delete_row"}:
        r = _int(p.get("row"), "row", 0, len(rows) if op == "add_row" else len(rows) - 1)
        if op == "delete_row":
            if len(rows) <= 1:
                raise ValueError("不能删除表格最后一行")
            writer._stamp(etree.SubElement(_prop(rows[r], "trPr"), w("del")))
        else:
            if len(p["values"]) != cols:
                raise ValueError("新增行内容与列数不匹配")
            row = etree.Element(w("tr"))
            writer._stamp(etree.SubElement(_prop(row, "trPr"), w("ins")))
            for value, template in zip(p["values"], cells[min(r, len(rows) - 1)]):
                cell = etree.SubElement(row, w("tc"))
                props = template.find(w("tcPr"))
                if props is not None:
                    cell.append(copy.deepcopy(props))
                etree.SubElement(cell, w("p")).append(_run(value))
            if r == len(rows):
                table.append(row)
            else:
                rows[r].addprevious(row)
        return
    # Grid topology changes are reversible in the app via immutable-source
    # replay. Word's native support for merge/split/column undo is inconsistent;
    # keep explicit metadata/comments, never pretend these are text revisions.
    c = _int(p.get("column"), "column", 0, cols if op == "add_column" else cols - 1)
    if op in {"add_column", "delete_column"}:
        if op == "delete_column" and cols <= 1:
            raise ValueError("不能删除表格最后一列")
        if op == "add_column" and len(p["values"]) != len(rows):
            raise ValueError("新增列内容与行数不匹配")
        if op == "delete_column":
            grid.remove(grid.findall(w("gridCol"))[c])
            for row, rowcells in zip(rows, cells):
                row.remove(rowcells[c])
        else:
            col = copy.deepcopy(grid.findall(w("gridCol"))[min(c, cols - 1)])
            grid.insert(c, col)
            for row, rowcells, value in zip(rows, cells, p["values"]):
                cell = etree.Element(w("tc"))
                props = rowcells[min(c, cols - 1)].find(w("tcPr"))
                if props is not None:
                    cell.append(copy.deepcopy(props))
                etree.SubElement(cell, w("p")).append(_run(value))
                if c == cols:
                    row.append(cell)
                else:
                    rowcells[c].addprevious(cell)
    elif op == "merge_cells":
        r = _int(p.get("row"), "row", 0, len(rows) - 1)
        end = _int(p.get("end_column"), "end_column", c + 1, cols - 1)
        target = cells[r][c]
        _set(_prop(target, "tcPr"), "gridSpan", val=end - c + 1)
        widths = [int(col.get(w("w"), "0")) for col in grid.findall(w("gridCol"))]
        _set(_prop(target, "tcPr"), "tcW", w=sum(widths[c : end + 1]), type="dxa")
        for cell in cells[r][c + 1 : end + 1]:
            for paragraph in cell.findall(w("p")):
                target.append(paragraph)
            rows[r].remove(cell)
    elif op == "split_cell":
        r = _int(p.get("row"), "row", 0, len(rows) - 1)
        if c >= len(cells[r]):
            raise ValueError("单元格坐标越界")
        target = cells[r][c]
        span = target.find(f"{w('tcPr')}/{w('gridSpan')}")
        if span is None:
            raise ValueError("只支持拆分横向合并单元格")
        count = int(span.get(w("val")))
        if not 2 <= count <= cols:
            raise ValueError("合并跨度无效")
        target.find(w("tcPr")).remove(span)
        width_node = target.find(f"{w('tcPr')}/{w('tcW')}")
        width = (
            max(100, int(width_node.get(w("w"), "1000")) // count)
            if width_node is not None
            else 1000
        )
        _set(_prop(target, "tcPr"), "tcW", w=width, type="dxa")
        previous = target
        for _ in range(count - 1):
            cell = etree.Element(w("tc"))
            cell.append(copy.deepcopy(target.find(w("tcPr"))))
            etree.SubElement(cell, w("p"))
            previous.addnext(cell)
            previous = cell


def _sources(writer, p):
    source = writer.source
    root, name = None, None
    for path, raw in source.parts.items():
        if path.startswith("customXml/") and path.endswith(".xml"):
            parsed = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
            if parsed.tag == f"{{{B}}}Sources":
                if root is not None:
                    raise ValueError("文档包含多个引用数据源集合，需人工合并")
                root, name = parsed, path
    if root is None:
        n = 1
        while (
            f"customXml/item{n}.xml" in source.parts
            or f"customXml/itemProps{n}.xml" in source.parts
        ):
            n += 1
        name = f"customXml/item{n}.xml"
        root = etree.Element(f"{{{B}}}Sources", nsmap={"b": B})
        root.set("SelectedStyle", "/APASixthEditionOfficeOnline.xsl")
        root.set("StyleName", "APA")
        props = etree.Element(f"{{{DS}}}datastoreItem", nsmap={"ds": DS})
        props.set(f"{{{DS}}}itemID", "{" + str(uuid4()).upper() + "}")
        refs = etree.SubElement(props, f"{{{DS}}}schemaRefs")
        etree.SubElement(refs, f"{{{DS}}}schemaRef").set(f"{{{DS}}}uri", B)
        source.parts[f"customXml/itemProps{n}.xml"] = etree.tostring(
            props, xml_declaration=True, encoding="UTF-8"
        )
        rels = etree.Element(f"{{{PKG}}}Relationships", nsmap={None: PKG})
        etree.SubElement(
            rels,
            f"{{{PKG}}}Relationship",
            Id="rId1",
            Type=REL + "customXmlProps",
            Target=f"itemProps{n}.xml",
        )
        source.parts[f"customXml/_rels/item{n}.xml.rels"] = etree.tostring(
            rels, xml_declaration=True, encoding="UTF-8"
        )
        writer._add_content_type(
            f"/customXml/itemProps{n}.xml",
            "application/vnd.openxmlformats-officedocument.customXmlProperties+xml",
        )
    writer._add_relationship("../" + name, REL + "customXml")
    key, meta = p["bib_key"], p["metadata"]
    for entry in root.findall(f"{{{B}}}Source"):
        if entry.findtext(f"{{{B}}}Tag") == key:
            if (
                entry.findtext(f"{{{B}}}Title") != meta["title"]
                or entry.findtext(f"{{{B}}}Year") != meta["year"]
            ):
                raise ValueError("文献标识已绑定另一篇文献，禁止覆盖")
            return root
    entry = etree.SubElement(root, f"{{{B}}}Source")
    fields = {
        "Tag": key,
        "SourceType": "JournalArticle" if meta.get("journal") else "Report",
        "Guid": "{" + str(uuid4()).upper() + "}",
        "Title": meta["title"],
        "Year": meta["year"],
    }
    for tag, field in (("JournalName", "journal"), ("Pages", "pages"), ("StandardNumber", "doi")):
        if meta.get(field):
            fields[tag] = meta[field]
    if meta.get("doi"):
        fields["URL"] = "https://doi.org/" + meta["doi"]
    for tag, value in fields.items():
        etree.SubElement(entry, f"{{{B}}}{tag}").text = value
    author = etree.SubElement(etree.SubElement(entry, f"{{{B}}}Author"), f"{{{B}}}Author")
    people = etree.SubElement(author, f"{{{B}}}NameList")
    # Preserve the supplied author string; do not guess Western given names.
    for value in re.split(r"\s*[;；]\s*", meta["author"]):
        person = etree.SubElement(people, f"{{{B}}}Person")
        etree.SubElement(person, f"{{{B}}}Last").text = value
    source.parts[name] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return root


def _field(instruction, cached):
    # CT_RunTrackChange does not allow fldSimple children. Complex fields are
    # represented by ordinary runs, so the ENTIRE field can be rejected in Word.
    begin = etree.Element(w("r"))
    marker = etree.SubElement(begin, w("fldChar"))
    marker.set(w("fldCharType"), "begin")
    marker.set(w("dirty"), "true")
    code = etree.Element(w("r"))
    text = etree.SubElement(code, w("instrText"))
    text.text = instruction
    text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    separate = etree.Element(w("r"))
    etree.SubElement(separate, w("fldChar")).set(w("fldCharType"), "separate")
    end = etree.Element(w("r"))
    etree.SubElement(end, w("fldChar")).set(w("fldCharType"), "end")
    return [begin, code, separate, _run(cached), end]


def apply_operation(writer, item, block, *, bibliography=None):
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
    append_bib = None
    existing_bib = None
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
        else:
            instructions = " ".join(
                n.text or "" for n in writer.source.document_root.iter(w("instrText"))
            )
            instructions += " ".join(
                n.get(w("instr"), "") for n in writer.source.document_root.iter(w("fldSimple"))
            )
            if re.search(r"ZOTERO|MENDELEY|EN\.CITE|CSL_CITATION", instructions, re.I):
                raise ValueError("检测到第三方引用管理域，不能混入 Word 原生引用系统")
            sources = _sources(writer, p)
            cached = f"({p['metadata']['author']}, {p['metadata']['year']})"
            if action == "insert_citation":
                if clone.tag != w("p") or bibliography is None:
                    raise ValueError("引用正文或参考文献列表未可靠定位")
                runs = _range_runs(clone, item.get("before") or item.get("quote"))
                ins = writer._stamp(etree.Element(w("ins")))
                ins.extend(_field(" CITATION " + p["bib_key"] + " ", cached))
                runs[-1].addnext(ins)
                bib_anchor = bibliography.element
            else:
                bib_anchor = element
            if bib_anchor.tag != w("p") or bib_anchor.getparent() is None:
                raise ValueError("参考文献条目插入点不是正文段落")
            # A real BIBLIOGRAPHY field plus sources, not a dangling CITATION.
            # Do not replace another reference manager's or author's field.
            marker = next(
                (
                    n
                    for n in writer.source.document_root.iter(w("bookmarkStart"))
                    if n.get(w("name")) == "PaperFixerBibliography"
                ),
                None,
            )
            existing_bib = marker.getparent() if marker is not None else None
            append_bib = etree.Element(w("p"))
            bid = str(writer._id())
            start = etree.SubElement(append_bib, w("bookmarkStart"))
            start.set(w("id"), bid)
            start.set(w("name"), "PaperFixerBibliography")
            ins = writer._stamp(etree.SubElement(append_bib, w("ins")))
            field = _field(" BIBLIOGRAPHY ", "")
            cache = []
            for index, entry in enumerate(sources.findall(f"{{{B}}}Source")):
                if index:
                    br = etree.Element(w("r"))
                    etree.SubElement(br, w("br"))
                    cache.append(br)
                people = entry.findall(f".//{{{B}}}Last")
                record = {"author": "; ".join(n.text or "" for n in people)}
                for key, tag in (
                    ("title", "Title"),
                    ("year", "Year"),
                    ("journal", "JournalName"),
                    ("pages", "Pages"),
                    ("doi", "StandardNumber"),
                ):
                    record[key] = entry.findtext(f"{{{B}}}{tag}") or ""
                cache.append(_run(reference_text(record)))
            ins.extend(field[:3] + cache + field[-1:])
            end = etree.SubElement(append_bib, w("bookmarkEnd"))
            end.set(w("id"), bid)
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
        if append_bib is not None:
            if existing_bib is not None:
                existing_bib.getparent().replace(existing_bib, append_bib)
            else:
                bib_anchor.addnext(append_bib)
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
        raise ValueError("结构修改的 Word 块无法定位")
    return block


def preflight(source_bytes, item):
    from app.document.docx_model import DocxSource
    from app.document.docx_redline import RedlineWriter

    source = DocxSource(source_bytes)
    block = block_for(source, item)
    p = item.get("params") or {}
    bibliography = (
        block_for(source, {"span_label": p["bibliography_span_label"]})
        if p.get("bibliography_span_label")
        else None
    )
    apply_operation(RedlineWriter(source, comments=False), item, block, bibliography=bibliography)
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
