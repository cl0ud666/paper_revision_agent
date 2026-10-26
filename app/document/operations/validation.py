"""Validated edits against existing Word objects; disabled additions remain manual."""

import json
import re

ACTIONS = {
    "set_font_format",
    "set_paragraph_format",
    "table_edit",
    "insert_formula",
    "insert_paragraphs",
}
TABLE_OPS = {
    "set_cell_text",
    "delete_row",
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
    from app.document.operation_policy import disabled_operation_reason

    return disabled_operation_reason(action, p) or "不支持的结构操作"
