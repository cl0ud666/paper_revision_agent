"""
跨批次一致性检查（确定性，不依赖模型）。

要解决的问题：修改是分批做的，批 2 把"土豆"改成"马铃薯"，但批 5 所在的那段
还写着"土豆"——这种**同一概念两种写法**以前只能靠 Review 那一次全局判断，
而长文的 Review 视野会被预算截断。

这里用已经落地的补丁反推出"术语替换表"，再全文扫描一遍：

    「土豆」→「马铃薯」（来自 patch-012）
    → 但 P123、P456 里还写着「土豆」

两个来源：

1. **用户要求**：`user_rules` 里的 "把 X 写成 Y" / "X→Y" / "统一改为 Y" 这类写法；
2. **已应用的补丁**：before/replace 都很短（属于术语级替换）的那些。

只做"短片段替换"的检查（长句改写不算术语），因此误报很低。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.utils.text import normalize_whitespace

# 术语级替换的长度上限（超过就当成句子改写，不做一致性检查）
MAX_TERM_CHARS = 20

_RULE_PATTERNS = (
    re.compile(r"把\s*[「\"'“]?([^」\"'”\s，,。；;]{1,20})[」\"'”]?\s*(?:统一)?(?:写成|改成|改为|替换为|用)\s*[「\"'“]?([^」\"'”\s，,。；;]{1,20})"),
    re.compile(r"[「\"'“]?([^」\"'”\s，,。；;]{1,20})[」\"'”]?\s*(?:→|->|=>)\s*[「\"'“]?([^」\"'”\s，,。；;]{1,20})"),
)


def term_mappings_from_rules(rules: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """从用户要求里解析出术语替换表。"""

    mappings: list[dict[str, str]] = []
    for rule in rules or []:
        if rule.get("kind") != "rule" or rule.get("status", "active") != "active":
            continue
        text = str(rule.get("text") or "")
        for pattern in _RULE_PATTERNS:
            for old, new in pattern.findall(text):
                mappings.append(
                    {"old": old.strip(), "new": new.strip(), "source": str(rule.get("id") or "R")}
                )
    return mappings


def term_mappings_from_patches(patches: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """从已应用的补丁里提取术语级替换。"""

    mappings: list[dict[str, str]] = []
    for patch in patches or []:
        old = str(patch.get("before") or "").strip()
        new = str(patch.get("replace") or "").strip()
        if not old or not new:
            continue
        if len(old) > MAX_TERM_CHARS or len(new) > MAX_TERM_CHARS:
            continue
        if old == new:
            continue
        mappings.append(
            {
                "old": old,
                "new": new,
                "source": str(patch.get("patch_id") or ""),
                "span": str(patch.get("span_label") or ""),
            }
        )
    return mappings


def find_inconsistencies(
    document: str,
    *,
    mappings: Iterable[dict[str, str]],
    spans: Iterable[Any] | None = None,
    fixed_spans: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """
    全文扫描：已经统一成 B 的地方，是否还有地方写着 A。

    返回每个不一致项：{old, new, source, count, spans}
    """

    fixed = {str(label) for label in fixed_spans}
    span_list = list(spans or [])
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for mapping in mappings:
        old, new = mapping.get("old", ""), mapping.get("new", "")
        if len(old) < 2 or old == new:
            continue
        key = (old, new)
        if key in seen:
            continue
        if old not in document:
            continue

        hits: list[str] = []
        for span in span_list:
            text = getattr(span, "text", "")
            label = getattr(span, "label", "")
            if label in fixed:
                continue
            if old in text and new not in text:
                hits.append(label)

        total = document.count(old)
        if hits or total:
            seen.add(key)
            issues.append(
                {
                    "old": old,
                    "new": new,
                    "source": mapping.get("source", ""),
                    "count": total,
                    "spans": hits[:12],
                }
            )
    return issues


def describe_inconsistencies(items: Iterable[dict[str, Any]]) -> str:
    """转成给 Prompt / 报告看的文字。"""

    lines: list[str] = []
    for item in items:
        spans = "、".join(item.get("spans") or []) or "（全文）"
        lines.append(
            f"- 「{item['old']}」已在别处改为「{item['new']}」，"
            f"但 {spans} 仍写着「{item['old']}」（全文共 {item['count']} 处）"
        )
    return "\n".join(lines)
