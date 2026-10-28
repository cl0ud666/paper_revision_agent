"""Explicit user operations are independent of incidental audit findings."""

import re

from app.document.patches import Patch
from app.utils.text import looks_like_code

NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}
SIZES = {
    "小六": 6.5,
    "六号": 7.5,
    "小五": 9,
    "五号": 10.5,
    "小四": 12,
    "四号": 14,
    "小三": 15,
    "三号": 16,
    "小二": 18,
    "二号": 22,
}


def heading_level(span):
    """Numbered thesis headings may use Normal or a misleading template style."""
    if span.zone == "toc" or span.kind not in {"heading", "paragraph"}:
        return None
    text = re.sub(r"^#{1,6}\s+", "", span.text).strip()
    if "\n" in text or len(text) > 100 or re.search(r"[。！？；]|[.…·]{3,}|\t\d+\s*$", text):
        return None
    numbered = re.match(r"^(\d+(?:[.．]\d+)*)(?:\s+|(?=[\u4e00-\u9fff]))\S", text)
    if numbered:
        return len(re.split(r"[.．]", numbered[1]))
    hashes = re.match(r"^#{1,6}(?=\s)", span.text)
    return len(hashes[0]) if hashes and span.kind == "heading" else None


def _format_target(clause, spans):
    heading = re.search(r"([一二三四五六1-6])级标题|Heading\s*([1-6])|标题\s*([1-6])", clause, re.I)
    numbered = re.search(r"(?<![\d.])(\d+(?:[.．]\d+)+)(?![\d.])", clause)
    scope = bool(re.search(r"(?:同一|同|这[个种一]?|这一|该).{0,3}(?:层级|级别)|这类标题", clause))
    font = re.search(r"仿宋(?:_GB2312)?|宋体|黑体|楷体|微软雅黑|Times New Roman", clause, re.I)
    pt = re.search(r"(\d+(?:\.\d+)?)\s*(?:pt|磅|点)(?![A-Za-z])", clause, re.I)
    size = float(pt[1]) if pt else next((v for k, v in SIZES.items() if k in clause), None)
    if not (font or size) or re.search(r"不要|不改|无需|不用", clause):
        return None
    if heading:
        value = next(v for v in heading.groups() if v)
        level = int(value) if value.isdigit() else NUMBERS[value]
        matches = [s for s in spans if heading_level(s) == level]
    elif numbered:
        number = numbered[1].replace("．", ".")
        level = number.count(".") + 1
        matches = [
            s
            for s in spans
            if heading_level(s) == level
            and (
                scope
                or re.match(
                    r"^" + re.escape(number) + r"(?:\s|(?=[\u4e00-\u9fff]))",
                    re.sub(r"^#{1,6}\s+", "", s.text).replace("．", "."),
                )
            )
        ]
    else:
        return None
    params = {}
    if font:
        params["font_name"] = font[0]
    if size is not None:
        params["font_size_pt"] = size
    return level, params, matches


def request_text(state):
    decisions = [str(d.get("selected_option") or "") for d in state.get("selected_decisions") or []]
    return "\n".join(
        [
            state.get("user_request") or "",
            *[
                str(r.get("text") or "")
                for r in state.get("user_rules") or []
                if r.get("kind") == "rule"
                and r.get("status", "active") == "active"
                and not any(option and option in str(r.get("text") or "") for option in decisions)
            ],
        ]
    )


def explicit_targets(state, spans):
    text = request_text(state)
    result = []
    turn = int(state.get("turn", 1))
    # Commas commonly separate the target, font and size; don't lose the target.
    for clause in re.split(r"[。；;\n]", text):
        formatting = _format_target(clause, spans)
        if formatting:
            level, params, matches = formatting
            result.append(
                {
                    "id": -200000 - turn * 100 - level,
                    "source": "explicit_format",
                    "problem": clause.strip(),
                    "suggestion": clause.strip(),
                    "format_params": params,
                    "span_labels": [s.label for s in matches],
                    "evidence": matches[0].text if matches else "",
                    "category": "排版格式",
                }
            )
        citation = re.search(
            r"(?:新增|增加|添加|补充|加)\s*([一二两三四五六\d]+)?\s*(?:个|篇|条|处)?\s*(?:文献引用|参考文献|引用|文献)",
            clause,
        )
        if citation and not re.search(r"不要|不加|无需|不用", clause):
            value = citation[1] or "1"
            count = int(value) if value.isdigit() else NUMBERS.get(value, 1)
            anchors = [
                s
                for s in spans
                if s.kind == "paragraph" and s.zone == "body" and not looks_like_code(s.text)
            ]
            if not anchors:
                anchors = [
                    s for s in spans if s.kind == "paragraph" and not looks_like_code(s.text)
                ]
            refs = [s.label for s in spans if s.zone == "references"]
            anchor = anchors[0] if anchors else None
            result.append(
                {
                    "id": -200090 - turn * 100,
                    "source": "explicit_citation",
                    "problem": clause.strip(),
                    "suggestion": f"请人工新增 {count} 个与原文论据相关的文献引用，并核对来源、正文位置及文末条目。",
                    "requested_count": count,
                    "span_label": anchor.label if anchor else "",
                    "evidence": anchor.text if anchor else "",
                    "other_labels": refs,
                    "category": "学术规范",
                }
            )
        if re.search(
            r"(?:新建|新增|增加|添加|补充|插入|加)\s*(?:[一二两三四五六\d]+\s*(?:个|张)?)?\s*(?:\S{0,8}表格|表格|表(?!达|述|明|现))",
            clause,
        ) and not re.search(r"不要|不加|无需|不用", clause):
            anchor = next((s for s in spans if s.zone == "body"), None)
            result.append(
                {
                    "id": -200092 - turn * 100,
                    "source": "explicit_table",
                    "problem": clause.strip(),
                    "suggestion": "请人工新建所需表格并核对表头、数据、编号及正文引用。",
                    "span_label": anchor.label if anchor else "",
                    "evidence": anchor.text if anchor else "",
                    "category": "表格结构",
                }
            )
        if re.search(
            r"(?:补充|补做|新增|增加|添加|补齐|补).{0,20}(?:实验|消融)", clause
        ) and not re.search(r"不要|不补|无需|不用", clause):
            experiment_spans = [
                s
                for s in spans
                if s.zone == "body" and s.kind == "heading" and re.search(r"实验|消融", s.text)
            ]
            anchor = (
                experiment_spans[0]
                if experiment_spans
                else next((s for s in spans if s.zone == "body"), None)
            )
            result.append(
                {
                    "id": -200091 - turn * 100,
                    "source": "explicit_experiment",
                    "problem": clause.strip(),
                    "suggestion": clause.strip()
                    + "。生成实验结构和待填结果，未知数据用XXX并明确标注待开展/待填，保留真实数据。",
                    "span_label": anchor.label if anchor else "",
                    "evidence": anchor.text if anchor else "",
                    "other_labels": [
                        s.label
                        for s in spans
                        if anchor and anchor.index < s.index <= anchor.index + 8
                    ],
                    "category": "实验结构",
                }
            )
    return list({i["id"]: i for i in result}.values())


def format_patches(target, spans):
    return [
        Patch(
            patch_id="",
            span_id=s.id,
            span_label=s.label,
            issue_id=target["id"],
            action="set_font_format",
            params=dict(target["format_params"]),
            quote=re.sub(r"^#{1,6}\s+", "", s.text),
            rationale=target["problem"],
            category="排版格式",
        )
        for s in spans
        if s.label in target["span_labels"]
    ]
