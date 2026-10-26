"""Audit existing numeric citations. Adding references remains a manual task."""

import re

from app.document.spans import build_spans
from app.utils.text import looks_like_code


def citation_consistency_audit(document):
    """Only diagnose numeric citation systems; never invent or delete sources."""
    spans = build_spans(document)
    reference_start = next(
        (
            s.index
            for s in spans
            if re.fullmatch(r"(?:#+\s*)?(?:参考文献|references|bibliography)", s.text.strip(), re.I)
        ),
        None,
    )
    if reference_start is None:
        return []
    entries, cites = {}, []
    for span in spans:
        if looks_like_code(span.text):
            continue
        if span.index > reference_start:
            # A later heading ends the bibliography section.
            if span.kind == "heading":
                break
            m = re.match(r"^\s*\[(\d{1,4})\]", span.text)
            if m:
                entries.setdefault(int(m[1]), []).append(span)
        elif span.index < reference_start:
            for m in re.finditer(r"\[(\d{1,4}(?:\s*[-–,，]\s*\d{1,4})*)\]", span.text):
                values = []
                for token in re.split(r"[,，]", m[1]):
                    interval = re.split(r"[-–]", token)
                    if len(interval) == 2:
                        lo, hi = map(int, interval)
                        if 0 < hi - lo <= 100:
                            values.extend(range(lo, hi + 1))
                    else:
                        values.append(int(token))
                cites.extend((number, span, m.group()) for number in values)
    if not entries or not cites:
        return []  # Do not misclassify author-year citation styles.
    issues = []

    def add(span, problem, suggestion):
        issues.append(
            {
                "kind": "fix",
                "category": "学术规范",
                "severity": "medium",
                "source": "citation_consistency",
                "span_id": span.id,
                "span_label": span.label,
                "evidence": span.text[:120],
                "problem": problem,
                "suggestion": suggestion,
            }
        )

    missing_seen = set()
    for number, span, _ in cites:
        if number not in entries and number not in missing_seen:
            missing_seen.add(number)
            add(
                span,
                f"正文引用 [{number}] 在参考文献列表中没有对应条目",
                "请作者核对引用来源并补充对应条目；当前版本不自动新增引用或参考文献",
            )
    used = {n for n, _, _ in cites}
    for number, locations in entries.items():
        if len(locations) > 1:
            add(
                locations[-1],
                f"参考文献列表重复使用编号 [{number}]",
                "核对正文关联，只有对应关系明确才生成统一编号补丁",
            )
        if number not in used:
            add(
                locations[0],
                f"参考文献 [{number}] 未发现对应的正文数字引用",
                "核对正文是否确实使用该来源；有明确论据关联才添加引用，不凭编号缺失删文献",
            )
    encountered = []
    for number, span, _ in cites:
        if number in encountered:
            continue
        if encountered and number < max(encountered):
            add(
                span,
                f"数字引用首次出现顺序不递增：[{number}] 出现在 [{max(encountered)}] 之后",
                "结合论文规定的引用样式核对是否要求按首次出现排序；不可只改正文编号而不改参考文献对应关系",
            )
            break
        encountered.append(number)
    return issues
