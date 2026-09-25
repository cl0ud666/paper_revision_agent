"""
文档的可寻址切分。

为什么需要它：
现在的修改是“把原文片段拿去字符串匹配”，位置不稳、有歧义时只能猜。
这一层把文档切成**有稳定 ID 的块**（span），Agent 的每一条修改都必须引用 ID，
于是“定位、拒绝、回溯”才有依据。

ID 设计（两个层次）：

1. **稳定 ID**：``p3f9a2c1`` = 类型前缀 + 内容哈希前 8 位。
   好处：改动**别的**段落时，本段落的 ID 不变，历史补丁依然能对上；
   坏处：本段落被改后 ID 会变——所以我们会在补丁里记录
   ``span_id_after``，把新旧 ID 连成可回溯的链。
2. **可读标签**：``P07`` / ``H02`` / ``T01``，只在单次 Prompt 里使用，
   模型写标签比写哈希方便，我们负责把标签解析回稳定 ID。

句子级定位：span 内可以按句切分，得到 ``p3f9a2c1:S2`` 这样的句子 ID；
补丁引用到句级时，用“span + 句子序号 + 原文引用”三重校验，避免改错位置。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.utils.text import estimate_tokens, is_heading, normalize_whitespace

# 类型前缀（稳定 ID 用）与可读标签前缀
KIND_PREFIX = {"heading": "h", "paragraph": "p", "table": "t", "list": "l"}
LABEL_PREFIX = {"heading": "H", "paragraph": "P", "table": "T", "list": "L"}
KIND_NAMES = {"heading": "标题", "paragraph": "段落", "table": "表格", "list": "列表"}

# 文档分区：封面/目录这类内容默认不进入审查，避免"把封面当正文挑毛病"
ZONE_NAMES = {
    "cover": "封面",
    "front": "前置材料",
    "toc": "目录",
    "abstract": "摘要",
    "body": "正文",
    "references": "参考文献",
    "acknowledgement": "致谢",
    "appendix": "附录",
}

# 默认审查范围：摘要 + 正文（可改 PRA_AUDIT_SCOPE=body+refs / all）
AUDIT_SCOPES = {
    "body": {"abstract", "body"},
    "body+refs": {"abstract", "body", "references"},
    "all": set(ZONE_NAMES),
}

_COVER_KEYS = (
    "题目",
    "学院",
    "专业",
    "班级",
    "学号",
    "学生姓名",
    "指导教师",
    "导师",
    "完成日期",
    "本科毕业设计",
    "毕业设计",
    "分类号",
    "密级",
    "学校代码",
)
_FRONT_KEYS = ("诚信承诺", "原创性声明", "独创性声明", "授权书", "学位论文使用授权")
_TOC_KEYS = ("目录", "contents", "tableofcontents")
_ABSTRACT_KEYS = ("摘要", "abstract")
_REFS_KEYS = ("参考文献", "references", "bibliography")
_ACK_KEYS = ("致谢", "acknowledgement", "acknowledgments")
_APPENDIX_KEYS = ("附录", "appendix", "攻读学位期间", "作者简介", "简历")
_BODY_START_KEYS = ("引言", "绪论", "introduction")

_TOC_LINE_RE = re.compile(r"(?:\.{3,}|…{2,}|·{3,})\s*\d+\s*$")
_TOC_NUMBERED_RE = re.compile(r"^\d+(?:\.\d+)*\s+\S.*?\s?\d{1,3}$")
_PAGE_NUMBER_RE = re.compile(r"^\s*(?:第\s*\d+\s*页|\d{1,4}|[-–—]\s*\d{1,4}\s*[-–—])\s*$")
_SIGNATURE_RE = re.compile(r"^\s*(?:承诺人|签名|日期|年\s*月\s*日|学生签名|指导教师签名)")

_BLANK_RE = re.compile(r"^\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

# 句末标点后切分；“英文句号 + 空格 + 大写”也视作句末
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*|(?<=\.)\s+(?=[A-Z(])")


def split_sentences(text: str) -> list[str]:
    """把一段文本切成句子（中英文标点都支持）。"""

    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [part.strip() for part in parts if part and part.strip()]


def sentence_id(span_id: str, index: int) -> str:
    """句子 ID：``p3f9a2c1:S2``（index 从 1 开始）。"""

    return f"{span_id}:S{index}"


@dataclass
class Span:
    """文档里的一个可寻址块。"""

    id: str
    label: str
    kind: str
    section: str
    text: str
    start: int
    end: int
    index: int
    version: int = 1
    # 所属分区（封面/目录/摘要/正文/…）与是否值得审查
    zone: str = "body"
    auditable: bool = True
    no_reason: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def preview(self) -> str:
        text = normalize_whitespace(self.text)
        return text if len(text) <= 60 else text[:60] + "…"

    def sentences(self) -> list[str]:
        return split_sentences(self.text)

    def to_json(self, *, with_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "section": self.section,
            "chars": len(self.text),
            "tokens": self.tokens,
            "sentences": len(self.sentences()),
            "preview": self.preview,
            "start": self.start,
            "end": self.end,
            "version": self.version,
            "zone": self.zone,
            "auditable": self.auditable,
        }
        if self.no_reason:
            payload["no_reason"] = self.no_reason
        if with_text:
            payload["text"] = self.text
        return payload


class _BlockBuilder:
    """把文本按空行/标题切成块（保留原始偏移）。"""

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self._buffer: list[str] = []
        self._buffer_kind = "paragraph"
        self._start = 0
        self._offset = 0

    def feed(self, line: str) -> None:
        line_start = self._offset
        self._offset += len(line)
        stripped = line.rstrip("\n")

        if _BLANK_RE.match(line):
            self._flush(line_start)
            return

        kind = self._classify(stripped)
        if kind == "heading":
            self._flush(line_start)
            self.blocks.append(
                {"kind": "heading", "text": stripped.strip(), "start": line_start, "end": self._offset}
            )
            return

        # 类型变化（例如段落 -> 列表）也要断开
        if self._buffer and kind != self._buffer_kind:
            self._flush(line_start)

        if not self._buffer:
            self._start = line_start
            self._buffer_kind = kind
        self._buffer.append(stripped)

    def _flush(self, end_offset: int) -> None:
        if not self._buffer:
            return
        text = "\n".join(self._buffer).strip()
        if text:
            self.blocks.append(
                {
                    "kind": self._buffer_kind,
                    "text": text,
                    "start": self._start,
                    "end": end_offset,
                }
            )
        self._buffer = []

    def _classify(self, stripped: str) -> str:
        if is_heading(stripped):
            return "heading"
        if _TABLE_ROW_RE.match(stripped):
            return "table"
        if _LIST_RE.match(stripped):
            return "list"
        return "paragraph"

    def finish(self) -> list[dict[str, Any]]:
        self._flush(self._offset)
        return self.blocks


def _stable_id(kind: str, text: str, seen: dict[str, int]) -> str:
    """内容哈希 + 出现序号（相同内容的块也能各自区分）。"""

    digest = hashlib.sha1(normalize_whitespace(text).encode("utf-8")).hexdigest()[:8]
    base = f"{KIND_PREFIX.get(kind, 'p')}{digest}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def build_spans(text: str, *, version: int = 1) -> list[Span]:
    """
    把文档解析成 Spans。

    同一个文档版本反复解析会得到完全相同的 ID（幂等），
    因此补丁可以安全地按 ID 引用。
    """

    if not text or not text.strip():
        return []

    builder = _BlockBuilder()
    for line in text.splitlines(keepends=True):
        builder.feed(line)
    blocks = builder.finish()

    seen: dict[str, int] = {}
    counters: dict[str, int] = {}
    spans: list[Span] = []
    section = ""

    for index, block in enumerate(blocks):
        kind = block["kind"]
        if kind == "heading":
            section = block["text"].strip()
        counters[kind] = counters.get(kind, 0) + 1
        spans.append(
            Span(
                id=_stable_id(kind, block["text"], seen),
                label=f"{LABEL_PREFIX.get(kind, 'P')}{counters[kind]:02d}",
                kind=kind,
                section=section,
                text=block["text"],
                start=int(block["start"]),
                end=int(block["end"]),
                index=index,
                version=version,
            )
        )
    _classify_zones(spans)
    return spans


def _compact(text: str) -> str:
    """去掉空白（用于比对"摘    要"这种被拉开字距的标题）。"""

    return re.sub(r"\s+", "", text or "")


def _header_kind(text: str) -> str:
    """标题属于哪一类（用于分区切换）。"""

    compact = _compact(text).lower().strip("#*＊：:　.、")
    if compact in _TOC_KEYS:
        return "toc"
    if compact in _ABSTRACT_KEYS:
        return "abstract"
    if compact in _REFS_KEYS:
        return "references"
    if compact in _ACK_KEYS:
        return "acknowledgement"
    if compact in _APPENDIX_KEYS:
        return "appendix"
    if compact in _FRONT_KEYS:
        return "front"
    if any(key in compact for key in _COVER_KEYS) and len(compact) <= 12:
        return "cover"
    if any(compact.startswith(key) for key in _BODY_START_KEYS):
        return "body"
    return "other"


def _looks_like_toc_line(text: str) -> bool:
    """目录条目：标题 + 页码（点线或直接贴着页码）。"""

    compact = text.strip()
    if _TOC_LINE_RE.search(compact):
        return True
    if _TOC_NUMBERED_RE.match(compact):
        return True
    return bool(re.match(r"^[^。！？]{2,40}\s+\d{1,3}$", compact))


def _noise_reason(text: str) -> str:
    """明显不属于正文内容的块（页码、签名行、目录行）。"""

    stripped = text.strip()
    if not stripped:
        return "空"
    if _PAGE_NUMBER_RE.match(stripped):
        return "页码"
    if _SIGNATURE_RE.match(stripped):
        return "签名/日期行"
    if _looks_like_toc_line(stripped) and len(stripped) <= 60:
        return "目录行"
    return ""


def _classify_zones(spans: list[Span]) -> None:
    """
    给每个块定分区，并标出明显是噪声的块。

    规则很小但有针对性：

    - 正文开始之前的一切（封面、承诺页、目录）默认不进入审查；
    - 目录条目靠"标题 + 页码"识别，因此即使它长得像章节标题也不会污染正文；
    - 如果整篇都没找到正文（例如用户直接贴了一段没标题的文字），
      就退回"全部都算正文"，避免把内容整篇误判成前置材料。
    """

    current = "cover"
    body_started = False

    for span in spans:
        text = span.text
        if span.kind == "heading":
            kind = _header_kind(text)
            if not body_started:
                if kind == "toc":
                    current = "toc"
                elif kind == "abstract":
                    current = "abstract"
                    body_started = True
                elif kind in {"references", "acknowledgement", "appendix"}:
                    current = kind
                    body_started = True
                elif kind == "cover":
                    current = "cover"
                elif kind == "front":
                    current = "front"
                elif kind == "body":
                    current = "body"
                    body_started = True
                elif current == "toc" and _looks_like_toc_line(text):
                    current = "toc"
                else:
                    # 有点编号的章节标题（如 "2 相关工作"）就是正文开始
                    current = "body"
                    body_started = True
            else:
                if kind == "toc":
                    current = "toc"
                elif current == "toc" and _looks_like_toc_line(text):
                    current = "toc"  # 目录条目（长得像章节标题，其实是目录）
                elif kind in {"references", "acknowledgement", "appendix", "abstract"}:
                    current = kind
                else:
                    current = "body"
            span.zone = current
        else:
            if not body_started:
                span.zone = "toc" if _looks_like_toc_line(text) else current
                if current == "abstract":
                    body_started = True
            else:
                if current == "toc" and _looks_like_toc_line(text):
                    span.zone = "toc"
                else:
                    span.zone = current

        reason = _noise_reason(text)
        if reason:
            span.auditable = False
            span.no_reason = reason

    # 兜底：一条正文都没识别出来 → 不要把整篇当成前置材料
    if not any(span.zone in {"abstract", "body"} for span in spans):
        for span in spans:
            span.zone = "body"
            if span.no_reason in {"目录行"}:
                span.auditable = True
                span.no_reason = ""


def select_auditable(spans: Iterable[Span], scope: str = "body") -> list[Span]:
    """按审查范围筛出真正要送给 Audit 的块。"""

    allowed = AUDIT_SCOPES.get(scope) or AUDIT_SCOPES["body"]
    return [
        span
        for span in spans
        if span.zone in allowed and span.auditable
    ]


def structure_report(spans: Iterable[Span]) -> dict[str, Any]:
    """结构自检：各分区多少块、多少块被当作噪声跳过。"""

    spans = list(spans)
    zones: dict[str, int] = {}
    for span in spans:
        zones[span.zone] = zones.get(span.zone, 0) + 1

    return {
        "blocks": len(spans),
        "zones": zones,
        "headings": sum(1 for span in spans if span.kind == "heading"),
        "tables": sum(1 for span in spans if span.kind == "table"),
        "skipped_noise": sum(1 for span in spans if not span.auditable),
        "auditable": sum(
            1
            for span in spans
            if span.auditable and span.zone in {"abstract", "body"}
        ),
    }


def resolve_span(
    spans: Iterable[Span],
    *,
    span_id: str = "",
    label: str = "",
) -> Span | None:
    """按稳定 ID 优先、可读标签兜底地找到 span。"""

    spans = list(spans)
    if span_id:
        for span in spans:
            if span.id == span_id:
                return span
        # 允许模型只给短前缀
        for span in spans:
            if span.id.startswith(span_id):
                return span
    if label:
        cleaned = str(label).strip().upper()
        for span in spans:
            if span.label.upper() == cleaned:
                return span
        # 允许 "P7"、"p1" 这类没补零 / 大小写不规范的写法
        wanted = _split_label(cleaned)
        if wanted is not None:
            for span in spans:
                current = _split_label(span.label.upper())
                if current is not None and current[0] == wanted[0] and int(current[1]) == int(wanted[1]):
                    return span
    return None


def _split_label(label: str) -> tuple[str, str] | None:
    """把 "P01" 拆成 ("P", "1")；不是这种形式就返回 None。"""

    match = re.match(r"^([A-Z])(\d+)$", (label or "").strip().upper())
    if not match:
        return None
    return match.group(1), str(int(match.group(2)))


def render_outline(spans: Iterable[Span], *, max_spans: int = 60) -> str:
    """紧凑大纲（给 Orchestrator / Audit 头部看）。"""

    spans = list(spans)
    if not spans:
        return "（文档为空）"
    lines = []
    for span in spans[:max_spans]:
        prefix = f"{span.label}" if span.kind != "heading" else span.label
        lines.append(
            f"- {prefix} ({KIND_NAMES.get(span.kind, span.kind)}｜{span.tokens} tokens"
            f"｜{span.section or '开头'})：{span.preview}"
        )
    if len(spans) > max_spans:
        lines.append(f"- ……（其余 {len(spans) - max_spans} 个块已省略）")
    return "\n".join(lines)


def render_annotated(
    spans: Iterable[Span],
    *,
    with_sentences_for: Iterable[str] = (),
) -> str:
    """
    带 ID 的正文（给 Revision / Review 用）。

    被“点名”的 span 会额外标出句子编号（如 ``(2)``），
    这样模型可以精确到句（``P07:S2``），而其它块只显示块级标签以省 Token。
    """

    focus = {str(item) for item in with_sentences_for}
    lines: list[str] = []
    for span in spans:
        header = f"[{span.label}]"
        if span.kind == "heading":
            lines.append(f"{header} {span.text}")
            continue
        if span.id in focus or span.label in focus:
            sentences = span.sentences()
            body = " ".join(f"({i}) {sentence}" for i, sentence in enumerate(sentences, 1))
            lines.append(f"{header} {body}")
        else:
            lines.append(f"{header} {span.text}")
    return "\n\n".join(lines)


def index_summary(spans: Iterable[Span], *, limit: int = 200) -> list[dict[str, Any]]:
    """写进 State 的精简索引（不含全文）。"""

    return [span.to_json() for span in list(spans)[:limit]]


def spans_by_id(spans: Iterable[Span]) -> dict[str, Span]:
    return {span.id: span for span in spans}


def chunk_spans(
    spans: Iterable[Span],
    *,
    max_tokens: int = 1500,
    max_chunks: int | None = None,
) -> list[list[Span]]:
    """
    按 span 边界分块（供 Audit 逐块审查）。

    为什么不再按字符切：按字符切会把段落/表格从中间截断，
    模型引用的 span 标签就对不上了。按 span 分组可以保证
    “每个分块里的内容都是完整的、带 ID 的块”。
    """

    spans = list(spans)
    if not spans:
        return []

    max_tokens = max(200, int(max_tokens))
    chunks: list[list[Span]] = []
    current: list[Span] = []
    tokens = 0

    for span in spans:
        if current and tokens + span.tokens > max_tokens:
            chunks.append(current)
            current = []
            tokens = 0
        current.append(span)
        tokens += span.tokens
        if tokens >= max_tokens:  # 单个超大块自己成一块
            chunks.append(current)
            current = []
            tokens = 0

    if current:
        chunks.append(current)
    if max_chunks:
        chunks = chunks[:max_chunks]
    return chunks


@dataclass
class SpanDiff:
    """重新索引后的映射结果（用于回溯新旧 ID）。"""

    label_to_id: dict[str, str] = field(default_factory=dict)
    id_to_label: dict[str, str] = field(default_factory=dict)
