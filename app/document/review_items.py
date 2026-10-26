"""User-facing locations and explicit manual-work records (no model calls)."""

import re
from hashlib import sha256

from app.document.spans import build_spans


def location_map(document):
    return {
        s.label: (s.section.lstrip("# ").strip() or f"原文「{s.preview}」")
        for s in build_spans(document or "")
    }


def readable(text, locations):
    # Internal labels are never page numbers. Unknown/old labels stay unlocated.
    return re.sub(
        r"(?<![A-Za-z0-9_])[PHTL]\d+(?![A-Za-z0-9_])",
        lambda m: locations.get(m.group(), "原文相关位置"),
        str(text or ""),
    )


def public_item(item, document):
    locations = location_map(document)
    result = dict(item)
    for key in (
        "problem",
        "rationale",
        "reason",
        "suggestion",
        "blocked_reason",
        "title",
        "location",
        "position",
    ):
        if key in result:
            result[key] = readable(result[key], locations)
    result["position"] = locations.get(
        item.get("span_label"),
        result.get("position") or result.get("location") or "全文（具体位置待人工确认）",
    )
    if "options" in item:
        result["display_options"] = [readable(o, locations) for o in item["options"]]
    return result


def manual_reason(text):
    text = str(text or "")
    text = re.sub(
        r"(?:无需|不用|不要|不再|不)(?:补充|补做|补|添加|增加|开展|进行).{0,12}(?:实验|引用|文献)",
        "",
        text,
    )
    from app.document.operation_policy import CITATION_DISABLED, TABLE_DISABLED

    if re.search(r"(?:新增|增加|添加|补充|插入|补|加).{0,16}(?:引用|参考文献|文献)", text):
        return CITATION_DISABLED
    if re.search(
        r"(?:新建|新增|增加|添加|补充|插入|加).{0,16}(?:表格|数据表|结果表|对比表|行列)", text
    ):
        return TABLE_DISABLED
    # Missing measurements do not block drafting an experimental structure:
    # the author has authorized explicit XXX placeholders, never invented data.
    if re.search(r"(插入|新建|新增|添加|绘制|重建).{0,16}(图片|脚注)", text):
        return "当前工具未实现图片或脚注结构编辑，需要人工处理；不会重写或破坏已有对象"
    return ""


def manual_item(issue, reason, suggestion=None):
    title = (
        issue.get("problem") or issue.get("before") or issue.get("quote") or "需要人工处理的修改"
    )
    suggestion = (
        suggestion
        or issue.get("selected_option")
        or issue.get("suggestion")
        or "请结合原文人工完成此项修改"
    )
    key = "\n".join(str(x or "") for x in (title, issue.get("evidence"), suggestion, reason))
    return {
        **issue,
        "manual_id": "manual-" + sha256(key.encode()).hexdigest()[:16],
        "kind": "manual",
        "title": title,
        "problem": title,
        "blocked_reason": reason,
        "reason": issue.get("rationale") or title,
        "suggestion": suggestion,
    }


def decision_for(issue, decisions):
    for decision in reversed(decisions or []):
        evidence = str(decision.get("evidence") or "")
        if (evidence and evidence == issue.get("evidence")) or (
            decision.get("issue_id") == issue.get("id")
            and decision.get("problem") == issue.get("problem")
        ):
            return decision
    return None


def decision_conflict(patch, decisions, spans):
    """Enforce manual decisions even if a later model uses a different issue ID."""
    span = next((s for s in spans if s.id == patch.span_id or s.label == patch.span_label), None)
    for d in decisions or []:
        evidence = d.get("evidence") or ""
        quote = patch.quote or ""
        associated = (
            patch.issue_id == d.get("issue_id")
            or (span is not None and evidence and evidence in span.text)
            or (quote and evidence and (quote in evidence or evidence in quote))
        )
        if not associated:
            continue
        if d.get("manual_reason"):
            return (
                "用户选择："
                + d["selected_option"]
                + "。"
                + d["manual_reason"]
                + "；禁止改用未选择的删除或替代方案"
            )
        if patch.delete and not re.search(r"删除|删去|移除|去掉", d.get("selected_option", "")):
            return "用户所选方案未授权删除原文，已阻止替换为删除方案"
    return ""
