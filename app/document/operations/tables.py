"""Edit existing cells and table layouts without flattening protected objects."""

import copy

from lxml import etree

from app.document.docx_model import w
from app.document.operations.validation import _int
from app.document.operations.xml import (
    _prop,
    _run,
    _set,
    _style,
    _text,
    _tracked_properties,
    _unsafe,
)
from app.utils.text import looks_like_code


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
    if op == "delete_row":
        r = _int(p.get("row"), "row", 0, len(rows) - 1)
        if len(rows) <= 1:
            raise ValueError("不能删除表格最后一行")
        writer._stamp(etree.SubElement(_prop(rows[r], "trPr"), w("del")))
        return
    # Column/merge changes are reversible by replaying the immutable original.
    c = _int(p.get("column"), "column", 0, cols - 1)
    if op == "delete_column":
        if cols <= 1:
            raise ValueError("不能删除表格最后一列")
        grid.remove(grid.findall(w("gridCol"))[c])
        for row, rowcells in zip(rows, cells):
            row.remove(rowcells[c])
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
