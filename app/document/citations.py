"""Citation consistency and independently verified bibliographic candidates.

Crossref verifies identity, not whether a paper supports a claim. The final
review explicitly retains that distinction. Only public citation metadata is
sent to Crossref, never manuscript text, credentials or uploaded files.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from urllib.parse import quote

import httpx

from app.document.spans import build_spans
from app.utils.text import looks_like_code
from app.utils.tracing import utc_now


def _normal(text):
    return re.sub(r"[\W_]+", "", html.unescape(re.sub("<[^>]+>", "", str(text)))).casefold()


def _doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value).strip(), flags=re.I)
    if not re.fullmatch(r"10\.\d{4,9}/[^\s<>\"\x00-\x1f]{1,200}", value, re.I):
        raise ValueError("候选 DOI 格式无效，未进行网络访问")
    return value


def _request(path, params=None):
    try:
        # Never follow model-provided URLs or redirect to arbitrary hosts.
        with httpx.Client(
            timeout=httpx.Timeout(12, connect=4),
            follow_redirects=False,
            headers={
                "User-Agent": "PaperFixer/1.0 (bibliography verification)",
                "Accept": "application/json",
            },
        ) as client:
            response = client.get("https://api.crossref.org/works" + path, params=params)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("文献核验服务响应过大")
            result = response.json()
            if not isinstance(result.get("message"), dict):
                raise ValueError("文献核验服务返回格式异常")
            return result["message"]
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise ValueError(
            "暂时无法核验候选文献（服务不可用、未收录或请求受限）；这不代表文献不存在"
        ) from exc


@lru_cache(maxsize=256)
def lookup_candidate(title, author, year, doi=""):
    """Cache successes only; exact normalized title + year + author required."""
    if doi:
        records = [_request("/" + quote(_doi(doi), safe=""))]
    else:
        records = (
            _request("", {"query.bibliographic": f"{title} {author} {year}", "rows": 3}).get(
                "items"
            )
            or []
        )
    matched = []
    for record in records:
        if not isinstance(record, dict):
            continue
        titles = record.get("title") or []
        if not any(_normal(t) == _normal(title) for t in titles):
            continue
        years = {
            str(parts[0])
            for key in ("published", "published-print", "published-online", "issued")
            for parts in (record.get(key) or {}).get("date-parts", [])
            if parts
        }
        if str(year) not in years:
            continue
        authors = [p for p in (record.get("author") or []) if isinstance(p, dict)]
        names = [
            " ".join(str(p.get(k) or "") for k in ("given", "family")).strip() or p.get("name", "")
            for p in authors
        ]
        # Require each supplied author to match a recorded author. Initials and
        # omitted coauthors are okay, conflicting surnames are not.
        requested = [
            s.strip()
            for s in re.split(r"[;；]", re.sub(r"\s*(?:et al\.?|等)\s*$", "", author, flags=re.I))
            if s.strip()
        ]
        if not requested or not authors:
            continue

        def matches(wanted, person, name):
            target, full, family = _normal(wanted), _normal(name), _normal(person.get("family", ""))
            if target == full or target == family:
                return bool(target)
            if not family:
                return False
            given = _normal(person.get("given", ""))
            initials = "".join(
                part[0] for part in re.findall(r"\w+", person.get("given", ""))
            ).casefold()
            if target.endswith(family):
                supplied = target[: -len(family)]
            elif target.startswith(family):
                supplied = target[len(family) :]
            else:
                return False
            return bool(supplied and (supplied == given or supplied == initials))

        if not all(
            any(matches(wanted, person, name) for person, name in zip(authors, names))
            for wanted in requested
        ):
            continue
        if doi and _doi(record.get("DOI", "")).casefold() != _doi(doi).casefold():
            continue
        matched.append((record, names))
    unique = {str(r.get("DOI", "")).casefold(): (r, names) for r, names in matched if r.get("DOI")}
    if len(unique) != 1:
        raise ValueError(
            "候选文献未唯一匹配到相同标题、作者和年份；需补充 DOI 或人工核验，不直接认定为虚假文献"
        )
    record, names = next(iter(unique.values()))
    canonical_doi = _doi(record["DOI"])
    metadata = {
        "title": html.unescape(record["title"][0]),
        "author": "; ".join(names),
        "year": str(year),
        "doi": canonical_doi,
    }
    if record.get("container-title"):
        metadata["journal"] = record["container-title"][0]
    if record.get("page"):
        metadata["pages"] = record["page"]
    return {
        "metadata": metadata,
        "source_url": "https://doi.org/" + canonical_doi,
        "record_url": "https://api.crossref.org/works/" + quote(canonical_doi, safe=""),
        "verified_at": utc_now(),
        "verification": "书目信息已核验；尚不代表已核验全文论据",
    }


def resolve_citation(item, document, request="", *, allow_lookup=True):
    """Called only on fresh model output, before persisting the review patch."""
    from app.document.structured import describe_operation, verify_source_evidence

    p = item.get("params") or {}
    p.pop("source_verification", None)  # Never trust a model's self-certification.
    try:
        verify_source_evidence(item, document, request)
        p["source_verification"] = {"verification": "书目信息来自原文或用户提供资料，未联网核验"}
        return
    except ValueError:
        if not allow_lookup:
            raise
    meta = p.get("metadata") or {}
    verified = lookup_candidate(
        meta.get("title", ""), meta.get("author", ""), meta.get("year", ""), meta.get("doi", "")
    )
    p["metadata"] = dict(verified["metadata"])
    p["source_verification"] = {k: v for k, v in verified.items() if k != "metadata"}
    p["source_evidence"] = "Crossref 书目记录：" + verified["record_url"]
    item["replace"] = describe_operation(item["action"], p)


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
                "结合上下文识别候选文献并核验来源；能唯一核验则生成引用/文献补丁，否则明确缺失资料，不猜编号或编造文献",
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
