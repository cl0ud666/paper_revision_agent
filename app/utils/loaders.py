"""
把用户上传的文件变成论文文本。

支持格式：

- ``.docx``：优先用 python-docx，保留标题层级（Heading 1/2/3 -> #/##/###）、
  列表（-> "- "）与表格（-> Markdown 表格），因为这些结构后面会被
  段落解析器（app/document/spans.py）识别成可寻址的块。
  没装 python-docx 时退回标准库 zipfile + XML 解析，同样能取到标题层级。
- ``.pdf``：优先 pypdf，其次 pdfplumber；扫描版（提取不到文字）会明确提示需要 OCR。
- ``.md`` / ``.txt``：自动识别编码（UTF-8 / UTF-8-BOM / GBK / GB18030 / Big5）。

设计取舍：中文论文常见"记事本另存为 ANSI（GBK）"，如果只按 UTF-8 读会整篇乱码，
所以编码按常见顺序试探；docx/pdf 的依赖都是可选的，缺了会给出可执行的提示，
而不是抛一个看不懂的 ImportError。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.document.docx_model import (
    DocxSource,
    DocxUnavailable,
    paragraph_to_markdown,
    style_id_to_name,
)
from app.utils.text import normalize_document_text

# 按“最可能是对的”顺序试：UTF-8 系列 -> 中文 GB 系列 -> Big5 -> 兜底
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")

# Word 的公式（OMML）与文档主体命名空间
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}
DOCX_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}
UNSUPPORTED_HINTS = {
    ".doc": "旧版 .doc 无法直接解析，请在 Word 里“另存为 .docx”后再上传。",
    ".wps": "WPS 的 .wps 格式无法直接解析，请另存为 .docx 或 .txt。",
    ".caj": "CAJ 格式无法直接解析，请导出为 PDF 或 Word。",
    ".tex": "LaTeX 源码请另存为 .txt 后上传（或先编译成 PDF）。",
}


@dataclass
class LoadedDocument:
    """一次加载的结果。"""

    text: str
    source: str = ""
    format: str = ""
    encoding: str = ""
    warnings: list[str] = field(default_factory=list)
    # 是否拿到了"文字 ↔ Word XML"的对应关系（能直接把改动写回原文件）
    can_write_back: bool = False
    docx_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def paragraphs(self) -> int:
        return len([part for part in self.text.split("\n\n") if part.strip()])

    def describe(self) -> str:
        parts = [f"{self.format or '文本'}"]
        if self.encoding:
            parts.append(f"编码 {self.encoding}")
        parts.append(f"{self.chars} 字符")
        parts.append(f"{self.paragraphs} 段")
        text = "，".join(parts)
        if self.can_write_back:
            text += "（可直接把改动写回 Word 原文件）"
        if self.warnings:
            text += "\n提示：" + "；".join(self.warnings)
        return text


class DocumentLoadError(ValueError):
    """文件无法读取（格式不支持、依赖缺失、内容为空等）。"""


def load_document(path: str | Path) -> LoadedDocument:
    """从磁盘读取论文文件。"""

    file_path = Path(path)
    if not file_path.exists():
        raise DocumentLoadError(f"找不到文件：{file_path}")
    if file_path.is_dir():
        raise DocumentLoadError(f"这是一个文件夹，请指定具体文件：{file_path}")

    data = file_path.read_bytes()
    return load_bytes(data, filename=file_path.name)


def load_bytes(data: bytes, *, filename: str = "") -> LoadedDocument:
    """从内存字节读取（界面/接口上传走这条路）。"""

    if not data:
        raise DocumentLoadError("文件内容为空。")

    suffix = Path(filename).suffix.lower()
    if suffix in DOCX_SUFFIXES or _looks_like_docx(data):
        return _load_docx(data, filename)
    if suffix in PDF_SUFFIXES or data[:4] == b"%PDF":
        return _load_pdf(data, filename)
    if suffix in UNSUPPORTED_HINTS:
        raise DocumentLoadError(UNSUPPORTED_HINTS[suffix])
    return _load_text(data, filename)


# ---------------------------------------------------------------- 纯文本


def _load_text(data: bytes, filename: str) -> LoadedDocument:
    text, encoding = decode_text(data)
    if not text.strip():
        raise DocumentLoadError("文件里没有可读取的文字内容。")
    warnings: list[str] = []
    if encoding not in {"utf-8", "utf-8-sig"}:
        warnings.append(f"文件不是 UTF-8（按 {encoding} 读取），如有乱码请另存为 UTF-8。")
    return LoadedDocument(
        text=_normalize(text),
        source=filename,
        format=Path(filename).suffix.lstrip(".") or "text",
        encoding=encoding,
        warnings=warnings,
    )


def decode_text(data: bytes) -> tuple[str, str]:
    """按常见编码顺序试探解码。"""

    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    # 全部失败：用 utf-8 容错解码，保证流程能继续，同时在报告里提示
    return data.decode("utf-8", errors="replace"), "utf-8(替换非法字节)"


# ---------------------------------------------------------------- DOCX


def _looks_like_docx(data: bytes) -> bool:
    return data[:2] == b"PK" and b"word/" in data[:4000]


def _load_docx(data: bytes, filename: str) -> LoadedDocument:
    """
    读取 .docx。

    优先走 :class:`DocxSource`（结构化读取）：它除了文字，还会记下
    "每段文字在 document.xml 里对应哪个节点、从第几个字到第几个字"，
    有了这个对应关系，改完才能直接把改动写回 Word 原文件（保留版式、
    图表、公式、编号）。

    读不到结构时（例如没装 lxml）退回纯文字解析：改还是能改，
    但产物只能是文字，不能回写。
    """

    warnings: list[str] = []
    text = ""
    can_write_back = False
    stats: dict[str, Any] = {}

    try:
        source = DocxSource(data)
        text = source.text
        stats = source.describe()
        can_write_back = True
        warnings.extend(source.warnings)
    except DocxUnavailable:
        warnings.append(
            "未安装 lxml，已用内置解析器读取：文字照样能改，"
            "但没法把改动写回 Word 原文件（想要修订痕迹 + 批注请先 "
            "pip install python-docx，或单独 pip install lxml）。"
        )
    except Exception as exc:  # noqa: BLE001 - 结构解析失败不该拖垮读取
        warnings.append(f"结构化解析失败（{exc}），改用内置解析器读取。")

    if not text.strip():
        try:
            text = _docx_text_python_docx(data)
        except ImportError:
            warnings.append("未安装 python-docx，已用内置解析器读取（格式还原略简单）。")
        except Exception as exc:  # noqa: BLE001 - 退回内置解析器
            warnings.append(f"python-docx 解析失败（{exc}），改用内置解析器。")

    if not text.strip():
        text = _docx_text_stdlib(data)
        if text.strip() and not warnings:
            warnings.append("已用内置解析器读取（保留标题层级与表格）。")

    if not text.strip():
        raise DocumentLoadError("这个 .docx 里没有提取到文字（可能是图片/公式为主的文档）。")

    return LoadedDocument(
        text=_normalize(text),
        source=filename,
        format="docx",
        warnings=warnings,
        can_write_back=can_write_back,
        docx_stats=stats,
    )


def _docx_text_python_docx(data: bytes) -> str:
    """用 python-docx 按文档顺序提取段落与表格。"""

    from docx import Document  # type: ignore
    from docx.oxml.ns import qn  # type: ignore
    from docx.table import Table  # type: ignore
    from docx.text.paragraph import Paragraph  # type: ignore

    document = Document(io.BytesIO(data))
    lines: list[str] = []

    def iter_blocks() -> Any:
        body = document.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield Table(child, document)

    for block in iter_blocks():
        if isinstance(block, Paragraph):
            text = _paragraph_text_with_math(block).strip()
            if not text:
                continue
            lines.append(_paragraph_to_markdown(text, _style_name(block)))
        else:  # Table
            rows = []
            for row in block.rows:
                cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    rows.append(cells)
            if rows:
                lines.append(_rows_to_markdown_table(rows))

    return "\n\n".join(lines)


def _paragraph_text_with_math(paragraph: Any) -> str:
    """
    取段落文字，**并保留公式**。

    为什么需要：python-docx 的 ``paragraph.text`` 只返回普通文本，
    Word 里用公式编辑器写的 OMML 公式会被整段丢掉，
    结果正文里出现"公式如下："后面什么都没有 —— 模型就会报"公式缺失"，
    而这其实是解析问题，不是论文问题。

    这里按文档顺序把普通文字与公式文字拼起来；公式用 ``【公式：…】`` 标出，
    既保住了内容，也让模型知道这是公式而不是缺失。
    """

    element = paragraph._element  # noqa: SLF001 - python-docx 没有公开接口
    parts: list[str] = []
    for node in element.iter():
        tag = node.tag
        if tag == f"{_W_NS}t":
            parts.append(node.text or "")
        elif tag in {f"{_M_NS}oMath", f"{_M_NS}oMathPara"}:
            math_text = "".join(child.text or "" for child in node.iter(f"{_M_NS}t")).strip()
            if math_text:
                parts.append(f"【公式：{math_text}】")
    if not parts:
        return paragraph.text or ""

    # oMathPara 会包含 oMath，避免重复输出
    text = "".join(parts)
    return re.sub(r"【公式：(.+?)】", lambda match: f"【公式：{match.group(1).strip()}】", text)


def _style_name(paragraph: Any) -> str:
    try:
        return str(paragraph.style.name or "")
    except Exception:  # noqa: BLE001
        return ""


def _paragraph_to_markdown(text: str, style: str) -> str:
    """把 Word 段落样式映射成 Markdown（下游靠这些标记切块）。

    规则只在 :mod:`app.document.docx_model` 里维护一份：
    读文件和回写 Word 必须用同一套映射，否则偏移对不上。
    """

    return paragraph_to_markdown(text, style)


def _rows_to_markdown_table(rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = "| " + " | ".join(normalized[0]) + " |"
    separator = "| " + " | ".join(["---"] * width) + " |"
    body = ["| " + " | ".join(row) + " |" for row in normalized[1:]]
    return "\n".join([header, separator, *body])


def _docx_text_stdlib(data: bytes) -> str:
    """
    不依赖第三方库的 .docx 读取（zipfile + XML）。

    保留段落与标题层级（w:pStyle val="Heading1"/"标题 1"），表格里的文字也会被取出。
    """

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise DocumentLoadError(f"这个文件不是有效的 .docx：{exc}") from exc

    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DocumentLoadError(f"这个 .docx 的正文 XML 无法解析：{exc}") from exc
    lines: list[str] = []

    for paragraph in root.iter(f"{namespace}p"):
        # 普通文字 + 公式文字（OMML），按文档顺序拼接
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t":
                parts.append(node.text or "")
            elif node.tag == f"{_M_NS}t":
                parts.append(node.text or "")
        text = "".join(parts)
        text = text.strip()
        if not text:
            continue
        style = ""
        style_node = paragraph.find(f"{namespace}pPr/{namespace}pStyle")
        if style_node is not None:
            style = style_node.get(f"{namespace}val") or ""
        lines.append(_paragraph_to_markdown(text, _style_to_name(style)))

    return "\n\n".join(lines)


def _style_to_name(style_id: str) -> str:
    """docx 里的 pStyle 是样式 ID（Heading1），转成可读名字。"""

    return style_id_to_name(style_id)


# ---------------------------------------------------------------- PDF


def _load_pdf(data: bytes, filename: str) -> LoadedDocument:
    text = ""
    warnings: list[str] = []
    engine = ""

    try:
        text = _pdf_text_pypdf(data)
        engine = "pypdf"
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"pypdf 读取失败（{exc}）。")

    if not text.strip():
        try:
            text = _pdf_text_pdfplumber(data)
            engine = "pdfplumber"
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"pdfplumber 读取失败（{exc}）。")

    if not text.strip():
        if not engine:
            raise DocumentLoadError(
                "无法读取 PDF：请先安装解析库（pip install pypdf），"
                "或把论文另存为 .docx / .txt 再上传。"
            )
        raise DocumentLoadError(
            "这个 PDF 没有提取到文字（多半是扫描版），需要先做 OCR，或直接上传 .docx 版本。"
        )

    if engine:
        warnings.append("PDF 提取可能丢失公式、表格与分栏版式；关键段落建议核对原文。")
    if len(text) < 200:
        warnings.append("提取到的文字很少，可能是扫描件或图片型 PDF。")

    return LoadedDocument(
        text=_normalize(text),
        source=filename,
        format="pdf",
        warnings=warnings,
    )


def _pdf_text_pypdf(data: bytes) -> str:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页失败不影响其它页
            pages.append("")
    return _join_pdf_pages(pages)


def _pdf_text_pdfplumber(data: bytes) -> str:
    import pdfplumber  # type: ignore

    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return _join_pdf_pages(pages)


def _join_pdf_pages(pages: list[str]) -> str:
    cleaned = []
    for page in pages:
        text = page.replace("\r\n", "\n").strip()
        if text:
            # PDF 里经常有行内硬换行：把“不是句末”的换行接回去，段落感更接近原文
            text = re.sub(r"(?<![。！？；.!?;:：])\n(?![\n\-*|#])", "", text)
            cleaned.append(text)
    return "\n\n".join(cleaned)


# ---------------------------------------------------------------- 通用


def _normalize(text: str) -> str:
    """统一换行、去掉行尾空白、压掉过多空行。"""

    return normalize_document_text(text)


def supported_extensions() -> str:
    return "，".join([".docx", ".pdf", ".md", ".txt"])
