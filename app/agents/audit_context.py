"""Build bounded evidence context for audit strategies.

These helpers select existing document text. Model calls and scheduling remain
in audit.py; issue validation lives in audit_findings.py.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.document.spans import Span, render_annotated
from app.utils.consistency import (
    collect_proper_names,
    describe_naming_variants,
)
from app.utils.text import looks_like_code, normalize_whitespace


def _consistency_hint(
    chunk_labels: set[str],
    *,
    naming: list[dict[str, Any]],
) -> str:
    """
    只挑"与本批次有关"的一致性线索放进上下文。

    全量塞进去有两个坏处：占掉审查正文的预算；同一个词会被每批评一次。
    所以只保留出现在本批次的术语写法线索。
    """

    def relevant(labels: list[str]) -> bool:
        return any(label in chunk_labels for label in labels)

    relevant_naming = [
        item
        for item in naming
        if relevant([label for labels in (item.get("variants") or {}).values() for label in labels])
    ]
    return describe_naming_variants(relevant_naming)


# ---------------------------------------------------------------- 专名核验

# 一次核验最多看多少个专名（清单很短，几十个词足够覆盖一篇论文）
MAX_NAME_CANDIDATES = 60


def _name_context_snippet(text: str, form: str, *, width: int = 26) -> str:
    """取专名附近的一小段原文，给模型判断"这是不是那个产品/方法"。"""

    index = text.find(form)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + len(form) + width)
    # 压成一行：清单是一条一行，原文里夹换行会把列表撑乱
    return normalize_whitespace(text[start:end])


def _name_candidates(
    spans: list[Span],
    *,
    exclude: set[str] | None = None,
    limit: int = MAX_NAME_CANDIDATES,
) -> list[dict[str, Any]]:
    """按文档顺序收集术语及具体段落；频次不参与判错或排序。"""
    blocked = exclude or set()
    stats = collect_proper_names(spans)
    candidates = []
    for form, stat in stats.items():
        if form in blocked:
            continue
        contexts = []
        for index, span in enumerate(spans):
            if span.label not in stat.labels or looks_like_code(span.text):
                continue
            # Each occurrence must be judged in its own paragraph and section.
            contexts.append(
                {
                    "span_label": span.label,
                    "section": span.section,
                    "paragraph": span.text,
                    "previous": spans[index - 1].text if index else "",
                    "next": spans[index + 1].text if index + 1 < len(spans) else "",
                }
            )
            if len(contexts) >= 4:
                break
        if contexts:
            candidates.append({"name": form, "contexts": contexts})
        if len(candidates) >= limit:
            break
    return candidates


def _render_name_list(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for item in candidates:
        lines.append(f"- 术语：{item['name']}")
        for context in item["contexts"]:
            lines.extend(
                [
                    f"  [{context['span_label']}] 章节：{context['section']}",
                    f"  前段：{context['previous'][:400]}",
                    f"  所在段落：{context['paragraph']}",
                    f"  后段：{context['next'][:400]}",
                ]
            )
    return "\n".join(lines)


_CONCLUSION_KEYS = ("结论", "总结", "结语", "conclusion", "conclusions", "summary")
_NUMBER_RE = re.compile(r"\d")
# 对照材料的大小上限，保证"小对照"真的小
MAX_COMPARISON_SENTENCES = 40
MAX_SECTION_TAIL_CHARS = 120
# 只取到二级标题（"## 3实验"）：三级小节太碎，每个都取末段会把材料撑大
MAX_CHAPTER_LEVEL = 2
# 「强指标词」：出现就几乎可以断定这句在讲结果数值 —— 优先收
_METRIC_STRONG = (
    "%",
    "％",
    "准确率",
    "精确率",
    "精度",
    "召回",
    "F1",
    "ACC",
    "AUC",
    "误报",
    "漏报",
    "识别率",
)
# 「弱指标词」：只是"可能和数值有关" —— 配额不够时先丢这些
_METRIC_WEAK = (
    "提升",
    "下降",
    "提高",
    "降低",
    "达到",
    "误差",
    "均值",
    "平均",
    "相比",
    "优于",
)


def _is_conclusion_span(span: Span) -> bool:
    section = re.sub(r"[\s#*·]+", "", str(getattr(span, "section", "") or ""))
    return any(key in section.lower() for key in _CONCLUSION_KEYS)


def _render_section_tails(spans: list[Span], *, skip_zones: set[str] | None = None) -> str:
    """
    各章的"标题 + 结尾一段"（截断）。

    用于对照"摘要是怎么承诺的、正文到底有没有产出"：
    章节标题能看出有没有对应的章节，结尾一段能看出这一章到底做了什么。

    两条过滤（都是踩过的坑）：

    - **代码注释不是章节标题**：论文里`# 将序列切分为两半`这种Python注释
      会被spans当成Markdown一级标题，于是每一条注释都变成一个"章"、
      还各自配一个"末段"。这里只认"以编号或白名单词开头"的真标题。
    - **表格不当末段**：章节末尾若是表格，整张Markdown表会被压成一行塞进材料。
    """

    skip = skip_zones or set()
    lines: list[str] = []
    tail: Span | None = None

    def flush() -> None:
        nonlocal tail
        if tail is None:
            return
        body = normalize_whitespace(tail.text)
        if len(body) > MAX_SECTION_TAIL_CHARS:
            body = body[:MAX_SECTION_TAIL_CHARS] + "…"
        lines.append(f"[{tail.label}] {body}")
        tail = None

    def heading_level(text: str) -> int:
        match = re.match(r"^(#{1,6})\s", str(text or ""))
        return len(match.group(1)) if match else 99

    for span in spans:
        if span.zone in skip:
            continue
        if span.kind == "heading":
            if not _is_chapter_heading(span.text):
                continue  # 代码注释型"标题"：跳过，也不打断当前章
            flush()
            lines.append(f"[{span.label}] {span.text}")
            continue
        if span.kind == "table":
            continue  # 表格不当"末段"
        section = getattr(span, "section", "") or ""
        if _is_chapter_heading(section) and heading_level(section) <= MAX_CHAPTER_LEVEL:
            tail = span
    flush()
    return "\n".join(lines)


# 真章节标题：`# 4实验设计与实现`、`## 2.2 MoCo对比学习框架`、`# 摘 要`…
# 而`# 将序列切分为两半`（Python注释）不匹配 —— 它没有编号也不是白名单词。
_CHAPTER_HEAD_RE = re.compile(
    r"^#{0,6}\s*(?:"
    r"\d+(?:\.\d+)*"  # 4 / 2.2 / 2.2.1
    r"|第[一二三四五六七八九十]"
    r"|摘\s*要|中\s*文\s*摘\s*要|英\s*文\s*摘\s*要|ABSTRACT|Keywords?"
    r"|目\s*录|Contents"
    r"|致\s*谢|Acknowledg"
    r"|参考文献|References?"
    r"|附\s*录|Appendix"
    r"|结\s*论|总\s*结|结\s*语|Conclusion"
    r"|诚\s*信"
    r")",
    re.IGNORECASE,
)


def _is_chapter_heading(text: str) -> bool:
    """这一行（或这一段所属的section）是不是真的章节标题。"""

    return bool(_CHAPTER_HEAD_RE.match(str(text or "").strip()))


def _render_numbered_sentences(
    spans: list[Span], *, limit: int = MAX_COMPARISON_SENTENCES
) -> tuple[str, int]:
    """
    正文里"含数字"的句子（不含表格）：专门用来和表格里的数值对照。

    返回``(文本, 因上限被丢弃的句数)``。

    两档优先级：先收**强指标词**（准确率/召回/F1/误报…）的句子，再收弱指标词
    （提升/相比/优于…）的。以前是一锅端按出现顺序取前N句，结果是
    "说了一句'优于'的废话"把"准确率96.67%"这种关键句挤出配额。
    """

    strong: list[str] = []
    weak: list[str] = []
    for span in spans:
        if span.kind == "table":
            continue
        for sentence in span.sentences():
            if not _NUMBER_RE.search(sentence):
                continue
            line = f"[{span.label}] {sentence}"
            if any(hint in sentence for hint in _METRIC_STRONG):
                strong.append(line)
            elif any(hint in sentence for hint in _METRIC_WEAK):
                weak.append(line)

    ordered = strong + weak
    # 0 = 不限。注意不能写成ordered[:0]（那会一句都不取，
    # 整组「正文数字 × 表格」对照会被静默跳过）。
    if limit <= 0:
        return "\n".join(ordered), 0
    kept = ordered[:limit]
    dropped = len(ordered) - len(kept)
    return "\n".join(kept), dropped


def _build_comparisons(auditable: list[Span]) -> list[dict[str, Any]]:
    """
    要做的定向对照：每一组都是"两份材料 + 只看什么"。

    关键点：材料都很短，而且**把要对照的两份摆在一起**。
    跨章节矛盾本质是对照任务——两句话隔着上万token时，模型两边都记不住。
    """

    comparisons: list[dict[str, Any]] = []
    abstract = [span for span in auditable if span.zone == "abstract"]
    conclusion = [span for span in auditable if _is_conclusion_span(span)]

    if abstract and conclusion:
        comparisons.append(
            {
                "key": "abstract_conclusion",
                "title": "摘要 × 结论",
                "focus": "数值、结论强度、方法与数据集数量是否一致",
                "left": render_annotated(abstract),
                "right": render_annotated(conclusion),
            }
        )

    if abstract:
        tails = _render_section_tails(auditable, skip_zones={"abstract", "references"})
        if tails.strip():
            comparisons.append(
                {
                    "key": "promise_outcome",
                    "title": "摘要的承诺 × 各章产出",
                    "focus": "摘要里说做了/提出了的东西，各章有没有对应内容",
                    "left": render_annotated(abstract),
                    "right": tails,
                }
            )

    numbers, dropped_numbers = _render_numbered_sentences(
        auditable, limit=get_settings().context.audit_compare_sentences
    )
    tables = render_annotated([span for span in auditable if span.kind == "table"])
    if numbers.strip() and tables.strip():
        comparisons.append(
            {
                "key": "numbers_tables",
                "title": "正文数字 × 表格",
                "focus": "同一指标、同一组实验的数值是否一致",
                "left": numbers,
                "right": tables,
                # 因40句上限被丢掉的句数 —— 必须报出来，不能装作查了全篇
                "dropped": dropped_numbers,
            }
        )
    return comparisons
