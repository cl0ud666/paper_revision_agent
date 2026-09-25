"""
长文本处理：Token 估算、章节切分、分块、截断、大纲提取。

这些能力是 Context 治理的基础：只有先把长论文“看清楚”，
才能决定给每个 Agent 看哪一部分。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


def normalize_document_text(text: str) -> str:
    """
    归一化整篇文档的换行与空白。

    这是"读文件"与"写回 Word"共用的一份规则：
    两边必须逐字节一致，段落偏移才能对上原文件里的文字。
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"

# 中日韩文字（粗略范围，足够做 Token 估算）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")
_WHITESPACE_RE = re.compile(r"\s+")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")

_HEADING_PATTERNS = (
    re.compile(r"^#{1,6}\s+\S"),
    re.compile(r"^第\s*\d+\s*[章节部分]"),
    re.compile(r"^\d+(?:\.\d+){0,3}\s*[、.．]?\s+\S"),
    re.compile(r"^[一二三四五六七八九十]+\s*[、.．]\s*\S"),
    re.compile(r"^chapter\s+\d+", re.IGNORECASE),
)

_KNOWN_HEADINGS = {
    "摘要",
    "中文摘要",
    "英文摘要",
    "abstract",
    "关键词",
    "keywords",
    "目录",
    "contents",
    "table of contents",
    "引言",
    "绪论",
    "introduction",
    "相关工作",
    "related work",
    "方法",
    "方法论",
    "材料与方法",
    "method",
    "methods",
    "methodology",
    "实验",
    "实验设置",
    "experiment",
    "experiments",
    "结果",
    "结果与分析",
    "results",
    "discussion",
    "讨论",
    "结论",
    "总结",
    "总结与展望",
    "结论与展望",
    "conclusion",
    "conclusions",
    "研究结论",
    "参考文献",
    "references",
    "bibliography",
    "致谢",
    "致 谢",
    "acknowledgement",
    "acknowledgements",
    "acknowledgments",
    "附录",
    "appendix",
    "攻读学位期间取得的研究成果",
    "作者简介",
    "原创性声明",
    "诚信承诺",
    "学位论文原创性声明",
}


def estimate_tokens(text: str | None) -> int:
    """
    粗略估算 Token 数。

    中文按 1 字 ≈ 1 token，其他字符按 4 字符 ≈ 1 token。
    只用于预算控制，不追求精确。
    """

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return cjk + math.ceil(other / 4)


def normalize_whitespace(text: str) -> str:
    """压缩空白字符。"""

    return _WHITESPACE_RE.sub(" ", text or "").strip()


def compact_text(text: str, max_chars: int) -> str:
    """按字符数截断，用于简短预览。"""

    if not text:
        return ""
    cleaned = normalize_whitespace(text)
    if 0 < max_chars < len(cleaned):
        return cleaned[:max_chars].rstrip() + "…"
    return cleaned


@dataclass
class Section:
    """论文中的一个章节。"""

    title: str
    body: str
    start: int
    end: int

    @property
    def text(self) -> str:
        if self.title:
            return f"{self.title}\n{self.body}".strip()
        return self.body.strip()

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def is_heading(line: str) -> bool:
    """判断一行是否是章节标题。"""

    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False

    # 中文标题常被人为拉开字距（"摘    要"、"目    录"、"致    谢"），
    # 先把空白去掉再比对，否则这些章节会退化成普通段落。
    compact = re.sub(r"\s+", "", stripped)
    lowered = compact.lower().strip("#*＊：:　.、")
    if lowered in _KNOWN_HEADINGS or compact.lower() in _KNOWN_HEADINGS:
        return True
    return any(pattern.match(stripped) for pattern in _HEADING_PATTERNS)


def split_sections(text: str) -> list[Section]:
    """
    按章节切分论文。

    识别 Markdown 标题、``1.2`` / ``一、`` / ``第3章`` / ``Chapter 3``
    以及“摘要 / 引言 / 结论 / References”等常见标题。
    """

    if not text or not text.strip():
        return []

    sections: list[Section] = []
    offset = 0
    current_title = ""
    current_start = 0
    current_body: list[str] = []

    def flush(end_offset: int) -> None:
        body = "".join(current_body).strip()
        if current_title or body:
            sections.append(Section(current_title, body, current_start, end_offset))

    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        if is_heading(line):
            flush(line_start)
            current_title = line.strip()
            current_body = []
            current_start = line_start
        else:
            current_body.append(line)

    flush(offset)
    return [section for section in sections if section.text]


def locate_section(text: str, needle: str) -> str:
    """根据一段原文定位它所在的章节标题（用于问题的 location 字段）。"""

    if not needle:
        return ""
    position = text.find(needle)
    if position < 0:
        return ""
    title = ""
    for section in split_sections(text):
        if section.start <= position < section.end:
            title = section.title or "（正文开头）"
    return title


def document_outline(text: str, *, max_sections: int = 40, preview_chars: int = 80) -> str:
    """生成论文大纲（章节标题 + 预览 + 规模），供 Orchestrator 使用。"""

    sections = split_sections(text)
    if not sections:
        return "（未能识别出章节结构）"

    lines: list[str] = []
    for section in sections[:max_sections]:
        title = section.title or "（正文开头）"
        preview = compact_text(section.body, preview_chars)
        lines.append(f"- {title}（约 {section.tokens} tokens）：{preview}")
    if len(sections) > max_sections:
        lines.append(f"- ……（其余 {len(sections) - max_sections} 个章节已省略）")
    return "\n".join(lines)


def document_stats(text: str) -> str:
    """论文规模统计。"""

    sections = split_sections(text)
    paragraphs = [part for part in _PARAGRAPH_RE.split(text) if part.strip()]
    return (
        f"字符数 {len(text)}；估算 tokens {estimate_tokens(text)}；"
        f"识别章节 {len(sections)} 个；段落 {len(paragraphs)} 个"
    )


def split_chunks(
    text: str,
    *,
    max_tokens: int = 1500,
    overlap_tokens: int = 0,
    max_chunks: int | None = None,
) -> list[str]:
    """
    把长论文切成不超过 max_tokens 的块。

    先按段落聚合，尽量不切断段落；只有单段过长时才按句子硬切。
    """

    if not text or not text.strip():
        return []

    max_tokens = max(200, int(max_tokens))
    paragraphs = [part for part in _PARAGRAPH_RE.split(text) if part.strip()]
    chunks: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if buffer:
            chunks.append("\n\n".join(buffer).strip())
            buffer = []
            buffer_tokens = 0

    for paragraph in paragraphs:
        paragraph_tokens = estimate_tokens(paragraph)

        if paragraph_tokens > max_tokens:
            flush()
            chunks.extend(_hard_split(paragraph, max_tokens))
            continue

        if buffer and buffer_tokens + paragraph_tokens > max_tokens:
            flush()
            if overlap_tokens > 0 and chunks:
                tail = chunks[-1][-overlap_tokens * 2 :]
                buffer.append(tail)
                buffer_tokens = estimate_tokens(tail)

        buffer.append(paragraph)
        buffer_tokens += paragraph_tokens

    flush()

    if max_chunks and len(chunks) > max_chunks:
        chunks = chunks[:max_chunks]
    return chunks


def _hard_split(paragraph: str, max_tokens: int) -> list[str]:
    """把超长段落按句子切分。"""

    sentences = re.split(r"(?<=[。！？；.!?;])\s*", paragraph)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if not sentence:
            continue
        if buffer and estimate_tokens(buffer + sentence) > max_tokens:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        pieces.append(buffer)
    return [piece for piece in pieces if piece.strip()]


def truncate_middle(text: str, budget_tokens: int, *, head_ratio: float = 0.6) -> str:
    """在保留头尾的前提下截断文本（头尾通常更重要）。"""

    if not text:
        return ""
    current = estimate_tokens(text)
    if budget_tokens <= 0 or current <= budget_tokens:
        return text

    head_ratio = min(max(head_ratio, 0.1), 0.9)
    scale = budget_tokens / current
    head_chars = max(1, int(len(text) * scale * head_ratio))
    tail_chars = max(1, int(len(text) * scale * (1 - head_ratio)))

    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip()
    dropped = max(0, current - budget_tokens)
    return f"{head}\n\n……（此处省略约 {dropped} tokens）……\n\n{tail}"


def similarity(a: str, b: str) -> float:
    """文本相似度（0~1），用于问题去重。"""

    from difflib import SequenceMatcher

    left = normalize_whitespace(a).lower()
    right = normalize_whitespace(b).lower()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def find_duplicate_paragraphs(text: str, *, min_chars: int = 60) -> list[str]:
    """
    找出重复出现的段落（返回重复段落的开头片段）。

    修改阶段如果模型把某段“替换”成了自身加长版，很容易造成整段重复，
    这种问题必须被确定性检查抓到，而不是依赖模型自己发现。
    """

    if not text:
        return []

    seen: set[str] = set()
    duplicates: list[str] = []
    for paragraph in _PARAGRAPH_RE.split(text):
        key = normalize_whitespace(paragraph)
        if len(key) < min_chars:
            continue
        if key in seen:
            duplicates.append(compact_text(paragraph, 60))
        else:
            seen.add(key)
    return duplicates
