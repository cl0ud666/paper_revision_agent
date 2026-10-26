"""Small OOXML helpers shared by format and table operations."""

import copy
import re

from lxml import etree

from app.document.docx_model import w

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


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
