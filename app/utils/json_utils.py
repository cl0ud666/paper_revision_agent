"""
从 LLM 输出中稳健地抽取 JSON。

为什么需要这个模块：
LLM 即使被要求“只输出 JSON”，实际返回里也经常出现 ```json 代码块、
解释性前后缀、尾随逗号、单引号、Python 的 True/False 等。
如果直接 json.loads，整条 Workflow 会因为格式问题崩掉。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any


class ModelOutputError(ValueError):
    """模型输出无法解析成期望的结构。"""


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_PY_LITERALS = ((r"\bTrue\b", "true"), (r"\bFalse\b", "false"), (r"\bNone\b", "null"))


class _Missing:
    """内部哨兵值，表示“没有解析成功”。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<missing>"


_MISSING = _Missing()


def strip_code_fence(text: str) -> str:
    """去掉 Markdown 代码块围栏，只保留内容。"""

    if not text:
        return ""
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def iter_json_spans(text: str) -> list[str]:
    """
    扫描出所有“括号配对”的片段（跳过字符串内部的括号）。

    返回按出现顺序排列的候选片段。
    """

    spans: list[str] = []
    stack: list[str] = []
    start: int | None = None
    in_string = False
    quote = ""
    escaped = False

    pairs = {"{": "}", "[": "]"}
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in ("'", '"'):
            in_string = True
            quote = char
            continue
        if char in pairs:
            if not stack:
                start = index
            stack.append(pairs[char])
            continue
        if char in ("}", "]"):
            if stack and stack[-1] == char:
                stack.pop()
                if not stack and start is not None:
                    spans.append(text[start : index + 1])
                    start = None
            else:
                # 括号不匹配：放弃当前候选，重新开始
                stack.clear()
                start = None
    return spans


def repair(raw: str, *, python_literals: bool = False) -> str:
    """
    常见格式修复：BOM、中文引号、尾随逗号。

    python_literals=True 时额外把 True/False/None 转成 JSON 的写法，
    供 json.loads 使用；转成 Python 风格（literal_eval）时必须保持原样。
    """

    fixed = raw.strip().lstrip("\ufeff")
    fixed = fixed.replace("\u201c", '"').replace("\u201d", '"')
    fixed = fixed.replace("\u2018", "'").replace("\u2019", "'")
    fixed = _TRAILING_COMMA_RE.sub(r"\1", fixed)
    if python_literals:
        for pattern, replacement in _PY_LITERALS:
            fixed = re.sub(pattern, replacement, fixed)
    return fixed


def _try_loads(raw: str) -> Any:
    """
    依次尝试多种解析方式，任一种成功即返回。

    顺序很重要：先用标准 JSON，再用宽松修复，最后退回 Python 字面量。
    """

    for candidate in (raw, repair(raw), repair(raw, python_literals=True)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

    for candidate in (raw, repair(raw)):
        try:
            return ast.literal_eval(candidate)
        except (ValueError, SyntaxError, TypeError):
            continue

    return _MISSING


def extract_json(text: str) -> Any:
    """
    从任意文本中抽取第一个可解析的 JSON 值。

    Raises:
        ModelOutputError: 完全无法解析
    """

    if text is None:
        raise ModelOutputError("模型输出为空")

    candidates: list[str] = [text, strip_code_fence(text)]
    candidates.extend(iter_json_spans(text))
    stripped = strip_code_fence(text)
    if stripped != text:
        candidates.extend(iter_json_spans(stripped))

    for candidate in candidates:
        if not candidate or not candidate.strip():
            continue
        value = _try_loads(candidate)
        if value is not _MISSING:
            return value

    preview = text.strip()[:200]
    raise ModelOutputError(f"无法从模型输出中解析 JSON：{preview!r}")


def parse_json_object(
    text: str,
    *,
    required_keys: tuple[str, ...] = (),
    context: str = "",
) -> dict[str, Any]:
    """解析出一个 JSON 对象（dict）。"""

    value = extract_json(text)
    if isinstance(value, dict) and len(value) == 1:
        # 少数情况下模型会包一层 {"result": {...}}
        inner = next(iter(value.values()))
        if isinstance(inner, dict) and all(key in inner for key in required_keys):
            value = inner
    if not isinstance(value, dict):
        raise ModelOutputError(f"{context} 期望 JSON 对象，实际得到 {type(value).__name__}")
    missing = [key for key in required_keys if key not in value]
    if missing:
        raise ModelOutputError(f"{context} 缺少字段：{missing}")
    return value


def parse_json_list(
    text: str,
    *,
    key: str | None = None,
    context: str = "",
) -> list[Any]:
    """
    解析出一个 JSON 数组。

    如果模型返回 {"issues": [...]} 这种对象，可以用 key 指定字段名。
    """

    value = extract_json(text)
    if isinstance(value, dict):
        if key and isinstance(value.get(key), list):
            return value[key]
        for item in value.values():
            if isinstance(item, list):
                return item
        raise ModelOutputError(f"{context} 期望 JSON 数组，实际得到对象且不含数组字段")
    if not isinstance(value, list):
        raise ModelOutputError(f"{context} 期望 JSON 数组，实际得到 {type(value).__name__}")
    return value


def compact_json(value: Any, *, max_chars: int = 2000) -> str:
    """把对象压成紧凑 JSON，超长时截断（用于 Context 治理）。"""

    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    return text
