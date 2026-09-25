"""
把 LLM 给出的“修改建议”安全地落到论文文本上。

为什么不让 LLM 直接返回整篇新论文：

1. 长论文无法一次生成完整结果，容易丢内容；
2. 直接覆盖无法确认“到底改了什么”，也不好做 Review 和报告；
3. 一旦模型跑偏，整篇论文都被污染。

所以 Revision Agent 只输出“定位 + 替换”的编辑指令：

    {"issue_id": 2, "find": "<原文片段>", "replace": "<修改后片段>", "rationale": "..."}

这里负责把 find 在原文中**真正定位到**并替换，匹配策略从严格到宽松：

1. 精确匹配（唯一命中直接替换；多处命中时用 location 提示挑最近的）；
2. 归一化匹配（忽略空白与全/半角标点差异，再映射回原始位置）；
3. 段落级模糊匹配（相似度 ≥ 阈值且唯一命中）。

定位失败或存在歧义的编辑不会“硬改”，而是进入 failed 列表，
交给 Review Agent 判断，避免污染原文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

_WS_RE = re.compile(r"\s+")
_PUNCT_MAP = {
    "，": ",",
    "。": ".",
    "；": ";",
    "：": ":",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "、": ",",
    "～": "~",
    "－": "-",
    "—": "-",
}


@dataclass
class EditApplication:
    """应用编辑后的结果。"""

    document: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def applied_ids(self) -> list[int]:
        return [edit.get("issue_id") for edit in self.applied if edit.get("issue_id") is not None]

    @property
    def failed_ids(self) -> list[int]:
        return [edit.get("issue_id") for edit in self.failed if edit.get("issue_id") is not None]


def normalize_for_match(text: str) -> str:
    """归一化：去掉空白、统一标点。"""

    if not text:
        return ""
    mapped = "".join(_PUNCT_MAP.get(char, char) for char in text)
    return _WS_RE.sub("", mapped)


def _normalized_index_map(text: str) -> tuple[str, list[int]]:
    """返回 (归一化文本, 归一化字符 -> 原文下标 的映射)。"""

    chars: list[str] = []
    mapping: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(_PUNCT_MAP.get(char, char))
        mapping.append(index)
    return "".join(chars), mapping


def _occurrences(haystack: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = haystack.find(needle)
    while start >= 0:
        positions.append(start)
        start = haystack.find(needle, start + 1)
    return positions


def _nearest_position(document: str, needle: str, hint: str) -> int:
    """在多个命中里挑离 hint 最近的。"""

    hint_index = document.find(hint) if hint else -1
    positions = _occurrences(document, needle)
    if not positions:
        return -1
    if hint_index < 0:
        return positions[0]
    return min(positions, key=lambda position: abs(position - hint_index))


def _paragraph_candidates(document: str, needle: str, threshold: float = 0.88) -> list[tuple[int, int]]:
    """段落级模糊匹配，返回原文区间列表。"""

    normalized_needle = normalize_for_match(needle)
    if not normalized_needle:
        return []

    candidates: list[tuple[int, int, float]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", document):
        paragraph = match.group(0)
        normalized_paragraph = normalize_for_match(paragraph)
        if not normalized_paragraph:
            continue
        if abs(len(normalized_paragraph) - len(normalized_needle)) > max(
            40, len(normalized_needle)
        ):
            continue
        ratio = SequenceMatcher(None, normalized_paragraph, normalized_needle).ratio()
        if ratio >= threshold:
            candidates.append((match.start(), match.end(), ratio))

    candidates.sort(key=lambda item: item[2], reverse=True)
    return [(start, end) for start, end, _ in candidates]


def find_span(document: str, needle: str, *, location: str = "") -> tuple[int, int, str]:
    """
    定位 needle 在 document 中的区间。

    Returns:
        (start, end, strategy)；失败返回 (-1, -1, reason)
    """

    if not needle or not needle.strip():
        return -1, -1, "empty"

    # 1) 精确匹配
    positions = _occurrences(document, needle)
    if len(positions) == 1:
        return positions[0], positions[0] + len(needle), "exact"
    if len(positions) > 1:
        if location:
            index = _nearest_position(document, needle, location)
        else:
            index = positions[0]
        return index, index + len(needle), "exact-nearest"

    # 2) 归一化匹配（忽略空白 / 标点差异）
    normalized_doc, mapping = _normalized_index_map(document)
    normalized_needle = normalize_for_match(needle)
    if normalized_needle:
        norm_positions = _occurrences(normalized_doc, normalized_needle)
        if norm_positions:
            position = norm_positions[0]
            if len(norm_positions) > 1 and location:
                hint_start, _, _ = find_span(document, location)
                if hint_start >= 0:
                    position = min(
                        norm_positions,
                        key=lambda pos: abs(mapping[pos] - hint_start),
                    )
            start = mapping[position]
            end = mapping[position + len(normalized_needle) - 1] + 1
            return start, end, "normalized"

    # 3) 段落级模糊匹配
    candidates = _paragraph_candidates(document, needle)
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], "fuzzy-paragraph"
    if len(candidates) > 1:
        return -1, -1, "ambiguous"

    return -1, -1, "not_found"


def locate_unique(text: str, needle: str) -> tuple[int, int, str]:
    """
    严格唯一的定位（补丁校验专用）。

    与 :func:`find_span` 的区别：同一段文本里出现多次时**不猜**，
    直接返回 ``ambiguous``；这样宁可不改，也不会改错地方。

    Returns:
        (start, end, strategy)，失败时 start/end 为 -1。
    """

    if not needle or not needle.strip():
        return -1, -1, "empty"

    positions = _occurrences(text, needle)
    if len(positions) == 1:
        return positions[0], positions[0] + len(needle), "exact"
    if len(positions) > 1:
        return -1, -1, "ambiguous"

    # 归一化匹配（忽略空白与标点差异）
    normalized_doc, mapping = _normalized_index_map(text)
    normalized_needle = normalize_for_match(needle)
    if normalized_needle:
        positions = _occurrences(normalized_doc, normalized_needle)
        if len(positions) == 1:
            start = mapping[positions[0]]
            end = mapping[positions[0] + len(normalized_needle) - 1] + 1
            return start, end, "normalized"
        if len(positions) > 1:
            return -1, -1, "ambiguous"

    return -1, -1, "not_found"


def apply_edits(
    document: str,
    edits: Iterable[dict[str, Any]],
    *,
    allow_delete: bool = True,
) -> EditApplication:
    """
    顺序应用编辑指令。

    每条编辑都要能在**当前**文档里定位到，否则进入 failed 列表。
    """

    result = EditApplication(document=document)

    for raw_edit in edits:
        if not isinstance(raw_edit, dict):
            result.failed.append({"issue_id": None, "reason": "edit_not_object", "edit": raw_edit})
            continue

        find = raw_edit.get("find") or raw_edit.get("original") or ""
        replace = raw_edit.get("replace")
        if replace is None:
            replace = raw_edit.get("revised") or ""
        issue_id = raw_edit.get("issue_id")
        location = raw_edit.get("location") or ""

        if not isinstance(find, str) or not isinstance(replace, str):
            result.failed.append(
                {"issue_id": issue_id, "reason": "invalid_type", "edit": raw_edit}
            )
            continue

        if find.strip() == replace.strip():
            result.skipped.append(
                {"issue_id": issue_id, "reason": "no_change", "find": find, "replace": replace}
            )
            continue

        if not replace.strip() and not allow_delete:
            result.failed.append(
                {"issue_id": issue_id, "reason": "delete_not_allowed", "find": find}
            )
            continue

        start, end, strategy = find_span(result.document, find, location=location)
        if start < 0:
            result.failed.append(
                {
                    "issue_id": issue_id,
                    "reason": strategy,
                    "find": find,
                    "rationale": raw_edit.get("rationale", ""),
                    "suggestion": raw_edit.get("suggestion", ""),
                }
            )
            continue

        original = result.document[start:end]
        result.document = result.document[:start] + replace + result.document[end:]
        result.applied.append(
            {
                "issue_id": issue_id,
                "strategy": strategy,
                "original": original,
                "revised": replace,
                "rationale": raw_edit.get("rationale", ""),
                "suggestion": raw_edit.get("suggestion", ""),
                "range": [start, start + len(replace)],
            }
        )

    return result
