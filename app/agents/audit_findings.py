"""Normalize, verify and deduplicate audit findings without calling a model."""

from __future__ import annotations

import re
from typing import Any

from app.document.spans import Span, resolve_span
from app.state.schema import ISSUE_CATEGORIES, SEVERITY_ORDER
from app.utils.text import locate_section, looks_like_code, normalize_whitespace, similarity

SEVERITIES = ("high", "medium", "low")
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
MIN_EVIDENCE_CHARS = 8
SYSTEMATIC_SIMILARITY = 0.6


def _normalize_issue(
    item: Any,
    *,
    chunk: list[Span],
    document: str,
) -> dict[str, Any] | None:
    """规范化一条问题，并把段落标签解析成稳定 ID。"""

    if not isinstance(item, dict):
        return None

    problem = str(item.get("problem") or "").strip()
    if not problem:
        return None

    category = str(item.get("category") or "").strip()
    if category not in ISSUE_CATEGORIES:
        category = "语言表达"

    severity = str(item.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"

    # 三类：fix（确实写错，自动改）/ decision（前后冲突，需要作者拍板）/
    # advice（没写错但可以更好、以及所有代码问题，只写批注）。
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in ("fix", "decision", "advice"):
        kind = "fix"

    # decision 类给出可选项，让作者直接挑一个，不用自己组织语言
    raw_options = item.get("options") or []
    options: list[str] = []
    if isinstance(raw_options, list):
        for option in raw_options[:4]:
            text = str(option).strip()
            if text:
                options.append(text)

    # 矛盾的另一端：矛盾常常"报在 A、要改 B"（例如报在参数表 T10、要改正文 P183）。
    # 把另一处的段落 ID 一起带上，Revision 才能把那段正文放进上下文去改。
    raw_others = item.get("other_labels") or item.get("other") or []
    if isinstance(raw_others, str):
        raw_others = [piece.strip() for piece in raw_others.replace("，", ",").split(",")]
    other_labels: list[str] = []
    if isinstance(raw_others, list):
        for label in raw_others[:4]:
            text = str(label).strip()
            if text and text not in other_labels:
                other_labels.append(text)

    # 同一条问题涉及的**全部位置**（"不重复"指只写一条问题，不是只能有一个标号）
    raw_span_labels = item.get("span_labels") or []
    if isinstance(raw_span_labels, str):
        raw_span_labels = [piece.strip() for piece in raw_span_labels.replace("，", ",").split(",")]
    span_labels: list[str] = []
    if isinstance(raw_span_labels, list):
        for label in raw_span_labels[:60]:
            text = str(label).strip()
            if text and text not in span_labels:
                span_labels.append(text)

    evidence = str(item.get("evidence") or "").strip()
    label = str(item.get("span_label") or item.get("span_id") or "").strip()
    span = resolve_span(chunk, span_id=label, label=label)

    # 标签没写对时，退一步：按 evidence 找到它所在的块
    if span is None and evidence:
        for candidate in chunk:
            if evidence and evidence in candidate.text:
                span = candidate
                break
    if span is None and evidence:
        best, score = None, 0.0
        for candidate in chunk:
            current = similarity(candidate.text, evidence)
            if current > score:
                best, score = candidate, current
        if best is not None and score >= 0.6:
            span = best

    location = str(item.get("location") or "").strip()
    if not location:
        location = span.section if span is not None else locate_section(document, evidence)

    issue: dict[str, Any] = {
        "kind": kind,
        "options": options if kind == "decision" else [],
        "other_labels": other_labels if kind == "decision" else [],
        "span_labels": span_labels,
        "category": category,
        "severity": severity,
        "location": location,
        "problem": problem,
        "evidence": evidence,
        "suggestion": str(item.get("suggestion") or "").strip(),
        "span_id": span.id if span is not None else "",
        "span_label": span.label if span is not None else label,
        "source": "audit",
    }
    if span is None:
        issue["warning"] = "未能定位到具体段落"
    return issue


def _merge_issues(
    issues: list[dict[str, Any]], *, limit: int = 30, start_id: int = 1
) -> list[dict[str, Any]]:
    """合并跨批次的重复问题，按严重程度排序，并统一编号。"""

    from app.document.terminology import style_concept

    merged: list[dict[str, Any]] = []
    for issue in issues:
        duplicate = None
        for existing in merged:
            if (
                style_concept(issue)
                and style_concept(existing)
                and style_concept(issue) != style_concept(existing)
            ):
                continue
            if similarity(existing["problem"], issue["problem"]) >= 0.82:
                duplicate = existing
                break
            if (
                issue.get("evidence")
                and existing.get("evidence")
                and similarity(existing["evidence"], issue["evidence"]) >= 0.9
            ):
                duplicate = existing
                break

        if duplicate is None:
            merged.append(dict(issue))
            continue

        if SEVERITY_ORDER[issue["severity"]] < SEVERITY_ORDER[duplicate["severity"]]:
            duplicate["severity"] = issue["severity"]
        for key in ("suggestion", "evidence", "location", "span_id", "span_label"):
            if not duplicate.get(key) and issue.get(key):
                duplicate[key] = issue[key]
        # 同一条问题在不同地方被报出来时，把**位置并集**：
        # 全篇审核补充的位置和分块报的位置合到一起，Revision 才不会只改一处。
        labels: list[str] = []
        for source in (duplicate, issue):
            for label in [source.get("span_label")] + list(source.get("span_labels") or []):
                text = str(label or "").strip()
                if text and text not in labels:
                    labels.append(text)
        if labels:
            duplicate["span_labels"] = labels

    merged.sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], item.get("span_id", "")))
    for position, issue in enumerate(merged[:limit], start=start_id):
        issue["id"] = position
        issue["approved"] = False
    return merged[:limit]


def _screen(
    issues: list[dict[str, Any]],
    *,
    spans_by_id: dict[str, Span],
    min_severity: str = "medium",
    max_issues: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    审查后处理（这一步直接决定"会不会给一篇好论文报一堆问题"）：

    1. **逐字核对证据**：evidence 在对应段落里找不到 → 丢弃（防幻觉/防错位）；
    2. **同类系统性问题聚合**：同分类且问题描述高度相似的多条 → 合并成一条，
       并列出涉及的所有段落（"参考文献格式不统一（共 12 处：P581、P582…）"）；
    3. **严重程度门槛**：默认丢掉 low（可改可不改）；
    4. **总量上限**：超过上限时按严重程度截断。
    """

    meta = {
        "unverifiable": 0,
        "low_severity": 0,
        "merged_systematic": 0,
        "code_skipped": 0,
    }
    verified: list[dict[str, Any]] = []

    for issue in issues:
        span = spans_by_id.get(issue.get("span_id") or "")
        evidence = (issue.get("evidence") or "").strip()
        kind = issue.get("kind") or "fix"
        if span is None:
            meta["unverifiable"] += 1
            continue
        if re.search(
            r"大小写|变量名|命名", str(issue.get("problem", "")) + str(issue.get("suggestion", ""))
        ) and (re.search(r"[A-Za-z_]\w*_[A-Za-z_]\w*|`[^`]+`", evidence)):
            meta["code_skipped"] += 1
            continue
        # 代码段不自动改：工具读到的是抽取文本，上下文不全，判断不了代码对错。
        # 但也不丢——降级成"建议"，只在 Word 里留一条批注给作者看。
        if looks_like_code(span.text):
            meta["code_skipped"] += 1
            if re.search(
                r"大小写|拼写|写法|统一|变量名|命名",
                str(issue.get("problem", "")) + str(issue.get("suggestion", "")),
            ):
                continue
            issue["kind"] = "advice"
            kind = "advice"
        # 建议类不要求逐字引用原文（"建议补充 X 的来源"这种本来就没有可引的句子），
        # 但仍然要求定位到段落，批注才有地方挂。
        # 程序生成的条目（术语变体扫描）没有"逐字引用"这种东西——
        # 它的依据就是扫描结果本身，不该被逐字核对丢掉。
        if kind != "advice" and issue.get("source") not in (
            "naming_variants",
            "consistency",
        ):
            if len(evidence) < MIN_EVIDENCE_CHARS:
                meta["unverifiable"] += 1
                continue
            if normalize_whitespace(evidence) not in normalize_whitespace(span.text):
                meta["unverifiable"] += 1
                continue
        verified.append(issue)

    grouped = _merge_systematic(verified, meta)

    threshold = SEVERITY_RANK.get(min_severity, 1)
    kept = [
        issue
        for issue in grouped
        if SEVERITY_RANK.get(issue.get("severity", "medium"), 1) <= threshold
    ]
    meta["low_severity"] = len(grouped) - len(kept)

    kept.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 2), item.get("span_label", "")))
    return kept[:max_issues], meta


def _merge_systematic(issues: list[dict[str, Any]], meta: dict[str, int]) -> list[dict[str, Any]]:
    """把"同一类系统性毛病"合并成一条，避免同一个毛病被报 N 次。"""

    from app.document.terminology import style_concept

    groups: list[dict[str, Any]] = []
    for issue in issues:
        matched = None
        for group in groups:
            if (
                style_concept(issue)
                and style_concept(group)
                and style_concept(issue) != style_concept(group)
            ):
                continue
            if group["category"] != issue["category"]:
                continue
            if _same_kind(group["problem"], issue["problem"]):
                matched = group
                break
        if matched is None:
            item = dict(issue)
            item["affected_spans"] = [issue.get("span_label")] if issue.get("span_label") else []
            item["affected_ids"] = [issue.get("id")] if issue.get("id") is not None else []
            groups.append(item)
            continue

        if issue.get("span_label") and issue["span_label"] not in matched["affected_spans"]:
            matched["affected_spans"].append(issue["span_label"])
        if issue.get("id") is not None:
            matched["affected_ids"].append(issue["id"])
        # 保留更严重的等级
        if SEVERITY_RANK.get(issue["severity"], 2) < SEVERITY_RANK.get(matched["severity"], 2):
            matched["severity"] = issue["severity"]
        meta["merged_systematic"] += 1

    for group in groups:
        if len(group["affected_spans"]) > 1:
            others = "、".join(group["affected_spans"][1:6])
            suffix = f"（同类问题共 {len(group['affected_spans'])} 处，另见 {others}）"
            if suffix not in group["problem"]:
                group["problem"] = group["problem"].rstrip("。") + suffix
    return groups


def _same_kind(left: str, right: str) -> bool:
    """
    两条问题算不算"同一类系统性毛病"。

    只看整体相似度会漏：例如
      "参考文献格式不统一，缺少出版信息"
      "参考文献格式不统一，混用了两种著录方式"
    整体相似度不一定够，但开头完全一样。所以再加一条"共同前缀"判据。
    """

    left, right = left.strip(), right.strip()
    if not left or not right:
        return False
    if similarity(left, right) >= SYSTEMATIC_SIMILARITY:
        return True

    common = 0
    for first, second in zip(left, right):
        if first != second:
            break
        common += 1
    return common >= 6
