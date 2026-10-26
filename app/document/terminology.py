"""One evidence-backed language convention per training concept, before the gate.

These are aliases, not mandatory translations. No text is changed here.
Unknown concepts and actual factual/semantic disputes stay with normal audit.
"""

import re

from app.document.spans import build_spans
from app.utils.text import looks_like_code

CONCEPTS = {
    "batch_size": (
        "Batch Size",
        "每批样本的数量",
        ["批次大小", "批大小", "批量大小"],
        r"(?<![A-Za-z0-9_])batch\s+size(?![A-Za-z0-9_])|批次大小|批大小|批量大小",
    ),
    "batch": (
        "Batch",
        "一组样本",
        ["批次"],
        r"(?<![A-Za-z0-9_])batch(?!\s+size)(?![A-Za-z0-9_])|批次(?!大小)",
    ),
    "epoch": (
        "Epoch",
        "遍历完整训练集的一轮",
        ["训练轮次", "训练轮数"],
        r"(?<![A-Za-z0-9_])epochs?(?![A-Za-z0-9_])|训练轮次|训练轮数",
    ),
}


def style_concept(issue):
    problem = str(issue.get("problem") or "")
    if (
        issue.get("ignored")
        or issue.get("selected_option")
        or issue.get("kind") not in {"decision", "fix"}
    ):
        return None
    if issue.get("source") in {"naming_variants", "consistency"}:
        return None  # Keep deterministic spelling checks and explicit rules.
    if not re.search(r"统一|混用|不一致|术语规范", problem):
        return None
    if re.search(r"误译|误将|误用|混淆|错误地|实际含义|实验数据|数值冲突", problem):
        return None
    # The proposed choice determines the convention, not unrelated terms in
    # its surrounding explanation. This catches differently phrased duplicates.
    options = issue.get("options") or [issue.get("suggestion") or problem]
    text = " ".join(str(o) for o in options)
    found = []
    for key, (_, _, _, pattern) in CONCEPTS.items():
        if re.search(pattern, text, re.I):
            found.append(key)
    return found[0] if len(found) == 1 else None


def normalize_terminology_decisions(issues, document, rules=(), *, version=1):
    spans = [s for s in build_spans(document, version=version) if not looks_like_code(s.text)]
    groups, output = {}, []
    for issue in issues:
        key = style_concept(issue)
        if key is None:
            output.append(issue)
            continue
        english, meaning, chinese, pattern = CONCEPTS[key]
        # A real explicit convention is author authority. Do not replace it
        # with another choice or silently discard its requested change.
        if any(
            r.get("kind") == "rule"
            and r.get("status", "active") == "active"
            and re.search(r"统一|必须|一律", str(r.get("text") or ""))
            and re.search(pattern, str(r.get("text") or ""), re.I)
            for r in rules
        ):
            output.append(issue)
            continue
        if key in groups:
            groups[key]["merged_issue_ids"].append(issue.get("id"))
            continue
        matches = []
        for span in spans:
            for match in re.finditer(pattern, span.text, re.I):
                # A Chinese term followed by its English definition isn't
                # alternating language usage (e.g. 批大小（Batch Size）).
                is_english = bool(re.fullmatch(r"[A-Za-z\s]+", match.group()))
                aliases = "|".join(re.escape(term) for term in chinese)
                if (
                    is_english
                    and re.search(r"(?:" + aliases + r")[（(]\s*$", span.text[: match.start()])
                    and re.match(r"\s*[）)]", span.text[match.end() :])
                ):
                    continue
                matches.append((span, match.group()))
        # “批量” is ambiguous. Treat it as a size variant only where its
        # local wording explicitly denotes an amount, never for batch processing.
        if key == "batch_size":
            matches += [
                (s, "批量")
                for s in spans
                if re.search(
                    r"(?:增大|减小|扩大|缩小)(?:当前)?批量|批量(?:为|大小为?)\s*\d", s.text
                )
            ]
        forms = list(
            dict.fromkeys(
                english if re.fullmatch(r"[A-Za-z\s]+", form) else form for _, form in matches
            )
        )
        if len(forms) < 2:
            # A consistent existing translation isn't wrong just because it
            # differs from a prompt example. No convention needs confirming.
            groups[key] = {"merged_issue_ids": [issue.get("id")]}
            continue
        labels = list(dict.fromkeys(s.label for s, _ in matches))
        first = min((s for s, _ in matches), key=lambda s: s.index)
        terms = list(dict.fromkeys([english.lower(), *chinese, *forms]))
        options = [
            f"仅对表示“{meaning}”的术语，全文统一使用「{form}」；其他概念保持原意" for form in forms
        ]
        preferred = next((form for form in forms if form != english), chinese[0])
        options.append(
            f"仅对表示“{meaning}”的术语，首次出现写「{preferred}（{english}）」，后续统一使用「{preferred}」；其他概念保持原意"
        )
        if english not in forms:
            options.append(
                f"仅对表示“{meaning}”的术语，全文统一使用「{english}」；其他概念保持原意"
            )
        item = {
            **issue,
            "kind": "decision",
            "terminology_concept": key,
            "scope_terms": terms,
            "merged_issue_ids": list(issue.get("merged_issue_ids") or [issue.get("id")]),
            "problem": f"表示“{meaning}”的术语出现多种写法："
            + "、".join(f"「{f}」" for f in forms)
            + "。请确认全文采用哪种口径；不同概念不互相替换。",
            "evidence": first.text,
            "span_id": first.id,
            "span_label": first.label,
            "span_labels": labels,
            "options": options,
            "suggestion": "由作者选择全文术语口径，不预设唯一正确译名",
        }
        groups[key] = item
        output.append(item)
    return output
