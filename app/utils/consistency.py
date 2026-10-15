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
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------- 专有名词写法

# 只认"含字母的拉丁词"（模型名 / 数据集名 / 人名 / 缩写都在这一类）
# 标识符不是论文术语：不能把 FEWSHOT_QUERY_DIR 拆成 FEWSHOT/QUERY/DIR。
_LATIN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*(?![A-Za-z0-9_])"
)


def _name_key(token: str) -> str:
    """归一化用的键：大小写、连字符、点号都不算差别（Graph-SAGE == graphsage）。"""

    return re.sub(r"[.\-]", "", token).lower()


def _has_inner_capital(token: str) -> bool:
    """
    词里除了首字母还有大写吗？

    这一条是为了不误报普通英文词的句首大写（"We" vs "we"）：
    PubMed / Pubmed 这种大小写混排的专有名词才会命中。
    """

    return any(char.isupper() for char in token[1:])


def find_naming_variants(
    spans: Iterable[Any],
    *,
    min_occurrences: int = 2,
) -> list[dict[str, Any]]:
    """
    找"同一个专有名词写了两种样子"（确定性检查，不调用模型）。

    例：

        P03 里写 PubMed，T01 里写 Pubmed      -> 大小写不统一
        P05 里写 Graph-SAGE，P09 里写 GraphSAGE -> 连字符不统一

    为什么需要它：模型只能"看一段"，而专有名词写法是**全文属性**；
    这个扫描把证据先摆出来，模型再据此写成一条有据可依的问题，
    而不是凭印象改专有名词。

    只在"至少一种写法内部有大写"时才报告，避免把句首大写的普通词算进来。
    """

    forms: dict[str, dict[str, list[str]]] = {}
    for span in spans:
        text = getattr(span, "text", "") or ""
        label = str(getattr(span, "label", "") or "")
        for token in _LATIN_TOKEN_RE.findall(text):
            forms.setdefault(_name_key(token), {}).setdefault(token, []).append(label)

    items: list[dict[str, Any]] = []
    for key, variants in forms.items():
        if len(variants) < 2:
            continue
        if not any(_has_inner_capital(token) for token in variants):
            continue
        occurrences = sum(len(labels) for labels in variants.values())
        if occurrences < min_occurrences:
            continue
        items.append(
            {
                "key": key,
                "variants": {
                    token: sorted(set(labels)) for token, labels in sorted(variants.items())
                },
                "occurrences": occurrences,
            }
        )
    items.sort(key=lambda item: (-int(item["occurrences"]), str(item["key"])))
    return items


def describe_naming_variants(
    items: Iterable[dict[str, Any]],
    *,
    limit: int = 10,
    max_chars: int = 700,
) -> str:
    """
    转成给 Prompt 看的文字（每种写法给出出现过的段落）。

    做长度上限：这是"提示"而不是问题清单本身，不能占掉审查正文的预算。
    """

    lines: list[str] = []
    for item in list(items)[:limit]:
        parts = []
        for token, labels in list((item.get("variants") or {}).items())[:6]:
            where = "、".join(labels[:6]) or "（未定位）"
            parts.append(f"「{token}」（{where}）")
        lines.append("- 同一个词写作：" + " / ".join(parts))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n（其余略）"
    return text


# ---------------------------------------------------------------- 排版结构是否被改坏

_HEADING_LEAD_RE = re.compile(r"^#{1,6}[ \t]+")
_INTERIOR_MARKER_RE = re.compile(r"\s#{2,6}[ \t]+\S")


def find_structure_damage(spans: Iterable[Any]) -> list[dict[str, Any]]:
    """
    检查"排版标记被写进正文"这类结构损伤（确定性，不调用模型）。

    典型症状：

        ### ### 3.3.5 对比损失函数优化    <- 标记重复（模型把 ### 抄进了修改结果）
        ### 3.3.5 ### 对比损失函数优化    <- 标记跑到文字中间
        ###                              <- 标题只剩标记、文字没了

    为什么要单独拦一道：这类问题模型自己很难承认（它认为改得对），
    但在 Word 里就是真真切切的乱码，必须在复核阶段被确定性抓住。
    """

    items: list[dict[str, Any]] = []
    for span in spans:
        text = (getattr(span, "text", "") or "").strip()
        if not text or getattr(span, "kind", "") != "heading":
            continue
        body = _HEADING_LEAD_RE.sub("", text, count=1)
        label = str(getattr(span, "label", "") or "")
        if not body.strip():
            items.append({"label": label, "text": text[:60], "reason": "标题只剩下标记，文字丢了"})
        elif _HEADING_LEAD_RE.match(body):
            items.append(
                {
                    "label": label,
                    "text": text[:60],
                    "reason": "标题标记重复（模型把 ### 抄进了正文）",
                }
            )
        elif _INTERIOR_MARKER_RE.search(body):
            items.append(
                {
                    "label": label,
                    "text": text[:60],
                    "reason": "标题文字中间混进了 Markdown 标记",
                }
            )
    return items


def describe_structure_damage(items: Iterable[dict[str, Any]]) -> str:
    """转成报告 / Prompt 用的文字。"""

    return "\n".join(
        f"- {item['label']}：{item['reason']}（{item['text']}）" for item in items
    )


# ---------------------------------------------------------------- 专有名词清单

# 通用词与排版词：不算"论文里提到的方法或数据集"，避免误报
_NAME_STOPWORDS = frozenset(
    {
        "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
        "A", "AN", "THE", "AND", "OR", "OF", "IN", "ON", "TO", "FOR", "WITH", "USING",
        "WE", "OUR", "THIS", "THAT", "THESE", "THOSE", "IT", "IS", "ARE", "WAS",
        "TABLE", "TABLES", "FIG", "FIGS", "FIGURE", "FIGURES", "EQ", "EQS",
        "EQUATION", "SECTION", "SECTIONS", "CHAPTER", "APPENDIX", "REF", "REFS",
        "REFERENCE", "REFERENCES", "DOI", "URL", "HTTP", "HTTPS", "PDF", "DOCX",
        "TXT", "CSV", "XML", "JSON", "OK", "VS", "ET", "AL", "IE", "EG",
    }
)

# "总结性"位置：摘要与结论。这两处提到的方法/数据集，正文里应当出现过。
_CONCLUSION_KEYS = ("结论", "总结", "结语", "conclusion", "conclusions", "summary")

_LATIN_WORD_RE = _LATIN_TOKEN_RE


@dataclass
class NameStat:
    """一个专有名词在全文的分布。"""

    form: str
    count: int = 0
    labels: list[str] = field(default_factory=list)
    soft: int = 0  # 出现在摘要 / 结论的次数
    hard: int = 0  # 出现在正文其它部分的次数

    @property
    def is_summary_only(self) -> bool:
        return self.soft > 0 and self.hard == 0


def _is_proper_name(token: str, *, min_length: int = 3) -> bool:
    """
    看起来像"方法名 / 模型名 / 数据集名 / 缩写"吗？

    规则：全大写（BERT、GCN、ECS）或词内部有大写（PubMed、GraphSAGE、ResNet）。
    这条过滤把普通英文词（Table、We）与句首大写挡在外面，是压住误报的关键。
    """

    if len(token) < min_length:
        return False
    if token.upper() in _NAME_STOPWORDS:
        return False
    if token.isupper():
        return True
    return _has_inner_capital(token)


def _is_summary_span(span: Any) -> bool:
    """这段文字在不在"摘要 / 结论"这类总结性位置。"""

    if str(getattr(span, "zone", "") or "") == "abstract":
        return True
    section = re.sub(r"[\s#*·]+", "", str(getattr(span, "section", "") or ""))
    lowered = section.lower()
    return any(key in lowered for key in _CONCLUSION_KEYS)


def collect_proper_names(spans: Iterable[Any]) -> dict[str, NameStat]:
    """统计全文专有名词：出现次数、出现段落、在"摘要/结论"还是"正文"。"""

    stats: dict[str, NameStat] = {}
    for span in spans:
        text = str(getattr(span, "text", "") or "")
        label = str(getattr(span, "label", "") or "")
        summary_side = _is_summary_span(span)
        for token in _LATIN_WORD_RE.findall(text):
            if not _is_proper_name(token):
                continue
            stat = stats.get(token)
            if stat is None:
                stat = NameStat(form=token)
                stats[token] = stat
            stat.count += 1
            if label and label not in stat.labels:
                stat.labels.append(label)
            if summary_side:
                stat.soft += 1
            else:
                stat.hard += 1
    return stats


def find_cross_section_gaps(spans: Iterable[Any]) -> list[dict[str, Any]]:
    """
    只在摘要/结论出现、正文**从未**出现的专有名词。

    这就是"实验里没写 BERT，但结论里写了 BERT"那一类问题。
    为什么必须做成确定性检查：摘要和结论常常落在不同的审查批次里，
    模型一次只看一批，能不能发现完全取决于"这两句碰巧没碰巧分到一起"——
    所以同一篇论文跑三次可能只报一次。全文扫描不吃这个运气。

    给出的是"待核对"线索：要么正文补上对应的方法/实验，要么把总结里的名字去掉。
    """

    stats = collect_proper_names(spans)
    items = [
        {
            "name": stat.form,
            "count": stat.count,
            "labels": list(stat.labels),
            "summary_count": stat.soft,
        }
        for stat in stats.values()
        if stat.is_summary_only
    ]
    items.sort(key=lambda item: (-int(item["summary_count"]), str(item["name"])))
    return items


def _edit_distance_is_one(a: str, b: str) -> bool:
    """只判断编辑距离是不是 1（相邻字符交换算一次编辑）。"""

    if a == b:
        return False
    if len(a) == len(b):
        diff = [index for index, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff) == 1:
            return True
        if (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        ):
            return True  # 相邻交换：ESC <-> ECS
        return False
    if abs(len(a) - len(b)) != 1:
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    skipped = 0
    index = 0
    for char in long:
        if index < len(short) and char == short[index]:
            index += 1
        else:
            skipped += 1
            if skipped > 1:
                return False
    return skipped <= 1


def find_near_miss_names(
    spans: Iterable[Any],
    *,
    max_items: int = 6,
) -> list[dict[str, Any]]:
    """
    疑似拼错的专有名词：与另一个高频写法只差一个字符。

    例如"阿里云 ESC"（只出现 1 次）与"ECS"（出现 3 次）——
    这类错字模型很难发现（它不认识你的领域），但编辑距离能发现。
    """

    stats = collect_proper_names(spans)
    forms = sorted(stats)
    items: list[dict[str, Any]] = []
    for position, first in enumerate(forms):
        for second in forms[position + 1 :]:
            if abs(len(first) - len(second)) > 1:
                continue
            if first.upper() == second.upper():
                continue  # 只差大小写：已由"写法不统一"覆盖
            if any(char.isdigit() for char in first + second):
                continue  # 带数字的是编号/型号（H01、P12、V100），不是拼写
            lower_a, lower_b = first.lower(), second.lower()
            if lower_a + "s" == lower_b or lower_b + "s" == lower_a:
                continue  # 单复数差异不算错字
            if re.sub(r"\d", "", lower_a) == re.sub(r"\d", "", lower_b):
                continue  # 只差数字的是编号（H01/H02、P12/P13），不是拼写错误
            if not _edit_distance_is_one(first.upper(), second.upper()):
                continue
            stat_a, stat_b = stats[first], stats[second]
            rare, common = (
                (stat_a, stat_b) if stat_a.count <= stat_b.count else (stat_b, stat_a)
            )
            # 只提示"罕见写法"：出现一次的可疑写法 + 出现多次的常见写法
            if rare.count > 1 or common.count < 2:
                continue
            items.append(
                {
                    "suspected": rare.form,
                    "suspected_labels": list(rare.labels),
                    "suspected_count": rare.count,
                    "likely": common.form,
                    "likely_labels": list(common.labels)[:6],
                    "likely_count": common.count,
                }
            )
    items.sort(key=lambda item: -int(item["likely_count"]))
    return items[:max_items]


def describe_entity_check(
    gaps: Iterable[dict[str, Any]],
    near_misses: Iterable[dict[str, Any]],
) -> list[str]:
    """把上面两类结果转成 Prompt / 报告里的小标题段落。"""

    sections: list[str] = []
    miss_items = list(near_misses)
    # 疑似笔误的名字往往同时"正文没出现"，但那是笔误造成的假象：
    # 只留更准确的那条（疑似拼写），避免同一个问题报两遍。
    suspected_forms = {str(item.get("suspected") or "") for item in miss_items}
    gap_items = [item for item in gaps if str(item.get("name")) not in suspected_forms]
    if gap_items:
        lines = ["只在摘要或结论里出现、正文（方法/实验）从未出现的方法名/数据集名/缩写："]
        for item in gap_items[:10]:
            where = "、".join(item.get("labels") or []) or "（未定位）"
            lines.append(f"- 「{item['name']}」（出现在 {where}，正文从未出现）")
        sections.append("\n".join(lines))

    if miss_items:
        # 措辞中性：形近 ≠ 写错。GNN / KNN / CNN / RNN 都是真实存在的独立术语，
        # 只差一个字符不能说明其中一个是笔误——必须回到两处的上下文去判断。
        # （这里不写"疑似笔误"、也不强调出现次数，避免把模型推向"少数服从多数"。）
        lines = [
            "形近术语（两种写法只差一个字符）。**这只是提示，不代表其中哪个写错了**，"
            "请结合两处所在的上下文判断它们是不是同一个东西："
        ]
        for item in miss_items[:6]:
            suspected = "、".join(item.get("suspected_labels") or []) or "（未定位）"
            likely = "、".join(item.get("likely_labels") or []) or "（未定位）"
            lines.append(
                f"- 「{item['suspected']}」（出现在 {suspected}）"
                f" 与 「{item['likely']}」（出现在 {likely}）"
            )
        sections.append("\n".join(lines))
    return sections
