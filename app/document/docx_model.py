"""
Word (.docx) 的结构化读取层：**文字 + XML 锚点**。

为什么需要它：

现在的流程是「docx -> 一坨文字 -> 改文字 -> 输出文字」。文字改对了，
但原文件里的版式、字体、编号、图表、公式**在读取那一刻就丢了**，
所以没法把改动写回 Word。

这一层解决的问题是：论文里的每一段文字，都能回答
**"它在 document.xml 里对应哪个 <w:t>，从第几个字到第几个字"**。
有了这个对应关系，回写层就能只动那一段的 XML，其余内容原样不动。

关键设计：

1. **偏移即契约**：``DocxSource.text`` 必须与 ``app.utils.loaders`` 读出来的
   文字**逐字节相同**（同一份代码生成，所以不会漂移），
   于是段落解析（spans）算出来的字符偏移可以直接用到本文档的映射上。
2. **装饰是空隙**：Markdown 的 ``# `` / ``- `` / ``| --- |`` 是解析器加的，
   原文件里并不存在，因此它们在映射里被记为 ``gap``，不属于任何 XML 节点。
   补丁如果落在空隙上，回写层会拒绝它（而不是猜）。
3. **公式/换行/图片只标记不改**：公式（OMML）用 ``【公式：…】`` 占位，
   换行、制表符、图形对象都如实登记；回写层据此判断"这一刀能不能动"。
4. **接受全部修订后的视图**：已经带 ``<w:del>`` 的旧内容不计入文字，
   所以读出来的文字 == 用户在 Word 里点"全部接受"之后看到的样子。
"""

from __future__ import annotations

import bisect
import io
import re
import zipfile
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from app.utils.text import normalize_with_map

# 命名空间
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def w(tag: str) -> str:
    """WordprocessingML 标签的 Clark 记法。"""

    return f"{{{W_NS}}}{tag}"


def m(tag: str) -> str:
    """数学（OMML）标签的 Clark 记法。"""

    return f"{{{M_NS}}}{tag}"


def r_ns(tag: str) -> str:
    """关系（r:）标签的 Clark 记法。"""

    return f"{{{R_NS}}}{tag}"


class DocxUnavailable(RuntimeError):
    """缺少解析 .docx 所需的库（lxml）。"""


def _require_lxml() -> Any:
    try:
        from lxml import etree  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise DocxUnavailable(
            "写回 Word 需要 lxml（pip install lxml，或 pip install python-docx 会一起装上）。"
        ) from exc
    return etree


# ---------------------------------------------------------------- 补丁可动的最小单位


@dataclass
class Piece:
    """
    段落文字里的一小段，以及它在 XML 里的来源。

    ``start`` / ``end`` 是相对**所属块文本**的偏移（不是相对 XML）。
    """

    start: int
    end: int
    kind: str  # text / math / break / tab / object / gap
    rendered: str
    # rendered 对应 node.text 里的哪一段（裁剪后 rendered 可能是 node.text 的一部分）
    node_offset: int = 0
    node: Any = None
    run: Any = None
    owner: Any = None  # 所属 <w:p>

    @property
    def editable(self) -> bool:
        """只有普通的 <w:t> 文字可以被替换。"""

        return self.kind == "text" and self.node is not None and self.run is not None


@dataclass
class DocxBlock:
    """文档里的一个块：一个段落，或一张表格。"""

    index: int
    kind: str  # heading / paragraph / list / table
    text: str  # 写进扁平文档的文字（含 "# " 这类装饰）
    element: Any  # <w:p> 或 <w:tbl>
    pieces: list[Piece] = field(default_factory=list)
    obstacles: list[tuple[int, str]] = field(default_factory=list)
    start: int = 0  # 在扁平文档里的起点
    end: int = 0
    label: str = ""  # 表格：第几张；方便写进"需人工处理"说明

    @property
    def flags(self) -> set[str]:
        kinds = {piece.kind for piece in self.pieces if piece.kind not in {"text", "gap"}}
        if self.obstacles:
            kinds.add("object")
        return kinds

    def pieces_in(self, start: int, end: int) -> list[Piece]:
        """与 [start, end) 相交的片段。"""

        return [p for p in self.pieces if p.start < end and p.end > start]

    def obstacles_in(self, start: int, end: int) -> list[str]:
        """区间内部（不含端点）的图片等无文字对象。"""

        return [tag for offset, tag in self.obstacles if start < offset < end]


# ---------------------------------------------------------------- 样式

_HEADING_RE = re.compile(r"(?:heading|标题)\s*([1-9])", re.IGNORECASE)


def paragraph_to_markdown(text: str, style: str) -> str:
    """把 Word 段落样式映射成 Markdown（下游靠这些标记切块）。"""

    name = (style or "").strip()
    lowered = name.lower()

    match = _HEADING_RE.search(name)
    if match:
        level = int(match.group(1))
        return f"{'#' * min(level, 6)} {text}"
    if "title" in lowered or name in {"标题", "题目"}:
        return f"# {text}"
    if "list" in lowered or name in {"列表段落", "列表"}:
        return f"- {text}"
    return text


def paragraph_prefix(text: str, style: str) -> str:
    """段落被装饰掉的前缀（"# " / "- "）。"""

    decorated = paragraph_to_markdown(text, style)
    return decorated[: len(decorated) - len(text)] if text and decorated.endswith(text) else ""


def style_id_to_name(style_id: str) -> str:
    """docx 里的 pStyle 是样式 ID（Heading1 / 1 / a3），转成可读名字。"""

    match = re.match(r"^[Hh]eading([1-9])$", style_id or "")
    if match:
        return f"Heading {match.group(1)}"
    if style_id in {"Title", "标题"}:
        return "Title"
    if (style_id or "").startswith("List"):
        return "List Paragraph"
    return style_id or ""


def _read_style_names(styles_xml: bytes | None) -> dict[str, str]:
    """
    读 word/styles.xml，把 styleId 解析成样式名。

    为什么必须做：中文 Word / WPS 常常把"标题 1"的 styleId 写成 ``1``、``a3``
    这类编号，只看 ID 是认不出标题的；而认不出标题，段落分区、
    审查范围、Markdown 结构就全是错的。
    """

    if not styles_xml:
        return {}
    etree = _require_lxml()
    try:
        root = etree.fromstring(styles_xml)
    except Exception:  # noqa: BLE001 - 样式坏了不该拖垮读取
        return {}

    names: dict[str, str] = {}
    for style in root.findall(w("style")):
        style_id = style.get(w("styleId")) or ""
        name_node = style.find(w("name"))
        name = (name_node.get(w("val")) if name_node is not None else "") or ""
        if style_id:
            names[style_id] = name or style_id_to_name(style_id)
    return names


# ---------------------------------------------------------------- 文字与锚点


def _math_text(element: Any) -> str:
    """取 OMML 公式里的可见文字。"""

    return "".join(node.text or "" for node in element.iter(m("t"))).strip()


_OBJECT_TAGS = {w("drawing"), w("pict"), w("object"), w("fldChar"), w("sectPr")}


def _iter_paragraph_content(node: Any) -> Iterator[tuple[str, Any, str]]:
    """
    按文档顺序产出段落里 ``(kind, element, rendered_text)``。

    - ``w:del`` 里的内容已经"被删除"，不算在文字里（返回的不是 delText）；
    - ``m:oMathPara`` 只是容器，公式本身由里面的 ``m:oMath`` 产出，避免重复。
    """

    for child in node:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag in {w("pPr"), w("del"), w("moveFrom"), w("instrText"), w("delInstrText")}:
            continue
        if tag == w("t"):
            yield "text", child, child.text or ""
            continue
        if tag == m("oMath"):
            math_text = _math_text(child)
            if math_text:
                yield "math", child, f"【公式：{math_text}】"
            continue
        if tag in {w("br"), w("cr")}:
            yield "break", child, "\n"
            continue
        if tag == w("tab"):
            yield "tab", child, "\t"
            continue
        if tag in _OBJECT_TAGS:
            yield "object", child, ""
            continue
        yield from _iter_paragraph_content(child)


class _TextBuilder:
    """一边拼文字、一边记下每个字符来自哪个 XML 节点。"""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pieces: list[Piece] = []
        self.obstacles: list[tuple[int, str]] = []
        self.length = 0

    def decor(self, text: str) -> None:
        """解析器加的装饰（Markdown 前缀、表格竖线）——不可编辑。"""

        self._append(text, "gap", None, None, None)

    def node(self, text: str, kind: str, node: Any, run: Any, owner: Any) -> None:
        self._append(text, kind, node, run, owner)

    def obstacle(self, kind: str, node: Any) -> None:
        """图片、域代码这类没有文字、但"占了位置"的对象。"""

        tag = str(node.tag).split("}", 1)[-1]
        self.obstacles.append((self.length, f"{kind}:{tag}"))

    def _append(self, text: str, kind: str, node: Any, run: Any, owner: Any) -> None:
        start = self.length
        end = start + len(text)
        if text:
            self.parts.append(text)
        self.pieces.append(
            Piece(
                start=start,
                end=end,
                kind=kind,
                rendered=text,
                node_offset=0,
                node=node,
                run=run,
                owner=owner,
            )
        )
        self.length = end

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def clip(self, start: int, end: int) -> list[Piece]:
        """裁到 [start, end) 并整体左移（用于去掉段落首尾空白）。"""

        clipped: list[Piece] = []
        for piece in self.pieces:
            left = max(piece.start, start)
            right = min(piece.end, end)
            if left >= right:
                continue
            offset = left - piece.start
            if piece.kind != "text":
                if offset or right - left != piece.end - piece.start:
                    # 非文字片被切断（理论上不会发生）：保守起见整块放弃映射
                    return []
                rendered = piece.rendered
            else:
                rendered = piece.rendered[offset : offset + (right - left)]
            clipped.append(
                Piece(
                    start=left - start,
                    end=right - start,
                    kind=piece.kind,
                    rendered=rendered,
                    node_offset=piece.node_offset + offset,
                    node=piece.node,
                    run=piece.run,
                    owner=piece.owner,
                )
            )
        return clipped


def _shift(pieces: list[Piece], delta: int) -> list[Piece]:
    if not delta:
        return pieces
    return [
        Piece(
            start=piece.start + delta,
            end=piece.end + delta,
            kind=piece.kind,
            rendered=piece.rendered,
            node_offset=piece.node_offset,
            node=piece.node,
            run=piece.run,
            owner=piece.owner,
        )
        for piece in pieces
    ]


def _normalize_block(block: DocxBlock) -> None:
    """
    把块的文本归一化成与整篇文档同一套规则，并把偏移一起搬到新坐标系。

    归一化会删字符（行尾空格、连续空行、首尾空白）。删了之后，块内每个
    piece / obstacle 的旧偏移就不再指向同一段文字——所以这里用
    ``normalize_with_map`` 拿到的位置映射，把它们逐个重算。

    只改数字，不碰任何 XML 节点。
    """

    raw = block.text
    normalized, kept = normalize_with_map(raw)
    if normalized == raw:
        return  # 绝大多数块走这条：零开销

    def remap(position: int) -> int:
        # kept 单调不减：bisect_left 得到"第一个原下标 ≥ position 的保留字符"的新下标
        return bisect.bisect_left(kept, position)

    pieces: list[Piece] = []
    for piece in block.pieces:
        start, end = remap(piece.start), remap(piece.end)
        if start < end:  # 整段都是空白、被裁没了的片段丢掉
            pieces.append(replace(piece, start=start, end=end))
    block.pieces = pieces
    block.obstacles = [(remap(offset), tag) for offset, tag in block.obstacles]
    block.text = normalized


def _paragraph_builder(paragraph: Any) -> _TextBuilder:
    builder = _TextBuilder()
    for kind, node, text in _iter_paragraph_content(paragraph):
        if kind == "object":
            builder.obstacle(kind, node)
        else:
            builder.node(text, kind, node, node.getparent(), paragraph)
    return builder


def paragraph_text(paragraph: Any) -> str:
    """一个段落的文字（含公式占位）——与扁平文档里看到的完全一致。"""

    return _paragraph_builder(paragraph).text


def _trim_bounds(text: str) -> tuple[int, int]:
    stripped = text.strip()
    if not stripped:
        return 0, 0
    start = len(text) - len(text.lstrip())
    return start, start + len(stripped)


def _paragraph_block(paragraph: Any, *, index: int, style: str) -> DocxBlock | None:
    builder = _paragraph_builder(paragraph)
    raw = builder.text
    start, end = _trim_bounds(raw)
    if start >= end:
        return None

    body = raw[start:end]
    pieces = builder.clip(start, end)
    if not pieces and body:
        # 无法建立映射（结构特殊）：仍然保留文字，但把可编辑范围标记为空
        pieces = []

    decorated = paragraph_to_markdown(body, style)
    prefix = decorated[: len(decorated) - len(body)] if decorated.endswith(body) else ""
    kind = (
        "heading" if prefix.startswith("#") else ("list" if prefix.startswith("-") else "paragraph")
    )

    shifted = _shift(pieces, len(prefix))
    obstacles = [
        (offset - start + len(prefix), tag)
        for offset, tag in builder.obstacles
        if start <= offset <= end
    ]
    return DocxBlock(
        index=index,
        kind=kind,
        text=decorated,
        element=paragraph,
        pieces=shifted,
        obstacles=obstacles,
    )


@dataclass
class _Cell:
    text: str
    pieces: list[Piece]
    obstacles: list[tuple[int, str]]


def _table_rows(table: Any) -> list[list[_Cell]]:
    """
    把 ``<w:tbl>`` 读成"每行每格的文字 + 锚点"。

    直接在 XML 上遍历 ``w:tr / w:tc``，不用 python-docx 的行列展开：
    后者会把横向合并的单元格重复展开，导致同一段文字出现两次
    （回写时就无法判断到底该改哪一个）。
    """

    rows: list[list[_Cell]] = []
    for row in table.findall(w("tr")):
        if row.find(f"{w('trPr')}/{w('del')}") is not None:
            continue
        cells: list[_Cell] = []
        for cell in row.findall(w("tc")):
            builder = _TextBuilder()
            paragraphs = cell.findall(w("p"))
            for position, paragraph in enumerate(paragraphs):
                if position:
                    builder.decor(" ")
                for kind, node, text in _iter_paragraph_content(paragraph):
                    if kind == "object":
                        builder.obstacle(kind, node)
                    else:
                        builder.node(text, kind, node, node.getparent(), paragraph)
            raw = builder.text
            start, end = _trim_bounds(raw)
            pieces = builder.clip(start, end)
            obstacles = [
                (offset - start, tag) for offset, tag in builder.obstacles if start <= offset <= end
            ]
            cells.append(_Cell(text=raw[start:end], pieces=pieces, obstacles=obstacles))
        rows.append(cells)
    return rows


def _table_block(table: Any, *, index: int, label: str) -> DocxBlock | None:
    """
    把 ``<w:tbl>`` 转成 Markdown 表格，**同时**记下每个单元格文字的锚点。

    只有"落在同一个单元格里"的改动才能写回 Word；
    跨单元格的改动（例如重排整行）会被回写层拒绝并提示人工处理，
    因为 Markdown 表格里的分隔符在 Word 里根本不存在，无法一一对应。
    """

    rows = _table_rows(table)
    if not rows or not any(cell.text.strip() for row in rows for cell in row):
        return None

    width = max(len(row) for row in rows) or 1
    builder = _TextBuilder()
    for row_index, row in enumerate(rows):
        if row_index:
            builder.decor("\n")
        builder.decor("| ")
        for column in range(width):
            if column:
                builder.decor(" | ")
            cell = row[column] if column < len(row) else None
            if cell is None or not cell.text:
                continue
            shift = builder.length
            builder.parts.append(cell.text)
            builder.pieces.extend(_shift(cell.pieces, shift))
            builder.obstacles.extend((offset + shift, tag) for offset, tag in cell.obstacles)
            builder.length += len(cell.text)
        builder.decor(" |")
        if row_index == 0:
            builder.decor("\n| ")
            builder.decor(" | ".join(["---"] * width))
            builder.decor(" |")

    return DocxBlock(
        index=index,
        kind="table",
        text=builder.text,
        element=table,
        pieces=builder.pieces,
        obstacles=builder.obstacles,
        label=label,
    )


# ---------------------------------------------------------------- 主入口


def _body_children(body: Any) -> Iterator[Any]:
    """正文里的段落与表格，按文档顺序；内容控件（sdt）里的也取出来。"""

    for child in body:
        tag = child.tag
        if tag in {w("p"), w("tbl")}:
            yield child
        elif tag == w("sdt"):
            content = child.find(w("sdtContent"))
            if content is not None:
                yield from _body_children(content)


class DocxSource:
    """
    一个 .docx 文件：既能读出"文字 + 锚点"，也能被回写层原地修改。

    用法::

        source = DocxSource(data)          # 读
        source.text                        # 与 loaders 读出来的文字完全一致
        block = source.block_at(offset)    # 某个字符偏移落在哪个段落/表格
        redline(source, patches, out_path) # 回写（见 docx_redline.py）
    """

    def __init__(self, data: bytes) -> None:
        etree = _require_lxml()
        self.etree = etree
        self.warnings: list[str] = []
        self.parts: dict[str, bytes] = {}

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    self.parts[info.filename] = archive.read(info.filename)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"这不是有效的 .docx：{exc}") from exc

        if "word/document.xml" not in self.parts:
            raise ValueError("这个 .docx 里找不到 word/document.xml（正文缺失）。")

        self.document_root = etree.fromstring(self.parts["word/document.xml"])
        self.styles = _read_style_names(self.parts.get("word/styles.xml"))
        self.blocks: list[DocxBlock] = []
        self.text = ""
        self.rebuild()

    # ---------- 读 ----------

    def rebuild(self) -> None:
        """（重新）从 XML 建立"文字 + 锚点"，并算出每个块的扁平偏移。"""

        self.blocks = self._collect_blocks()

        # 归一化会**删字符**（行尾空白、连续空行、首尾空白）。所以必须先归一化
        # **每个块的文本**、把 pieces/obstacles 的偏移一起搬过去，再按归一化后的
        # 长度累加块偏移。否则块的 start/end 与 self.text 会对不上，
        # 补丁定位（block_at）就会拿到前一个块，误报"跨了文档结构"。
        for block in self.blocks:
            _normalize_block(block)

        cursor = 0
        for position, block in enumerate(self.blocks):
            if position:
                cursor += 2  # 块之间用空行分隔
            block.start = cursor
            cursor += len(block.text)
            block.end = cursor

        joined = "\n\n".join(block.text for block in self.blocks)
        # 每个块的文本都已两端裁净、内部空行也已收敛，所以这里与
        # normalize_document_text(joined) **逐字节等价**（有测试盯着这一点）。
        self.text = joined + "\n" if joined else ""
        self._check_alignment()

    def _check_alignment(self) -> None:
        """
        自检：每个块的偏移都必须能在扁平文本里原样取回它自己的文字。

        正常情况下这里恒为空——`_normalize_block` 已经把偏移搬到了同一坐标系。
        真出现错位时，宁可把该块标成"只读不可回写"（让回写层拒绝），也不要改错。
        """

        broken = []
        for block in self.blocks:
            if self.text[block.start : block.start + len(block.text)] != block.text:
                block.pieces = []
                block.obstacles = []
                broken.append(block.index)
        if broken:
            self.warnings.append(f"有 {len(broken)} 个块无法与原文对齐，这些块只读不可回写。")

    def _collect_blocks(self) -> list[DocxBlock]:
        body = self.document_root.find(w("body"))
        if body is None:
            return []

        blocks: list[DocxBlock] = []
        table_counter = 0
        for element in _body_children(body):
            if element.tag == w("p"):
                style = self._paragraph_style(element)
                block = _paragraph_block(element, index=len(blocks), style=style)
            else:
                table_counter += 1
                block = _table_block(element, index=len(blocks), label=f"T{table_counter:02d}")
            if block is not None and block.text.strip():
                blocks.append(block)
        return blocks

    def _paragraph_style(self, paragraph: Any) -> str:
        properties = paragraph.find(w("pPr"))
        if properties is None:
            return ""
        style_node = properties.find(w("pStyle"))
        if style_node is None:
            return ""
        style_id = style_node.get(w("val")) or ""
        return self.styles.get(style_id) or style_id_to_name(style_id)

    # ---------- 定位 ----------

    def block_at(self, offset: int) -> DocxBlock | None:
        """某个扁平偏移落在哪个块里。"""

        for block in self.blocks:
            if block.start <= offset < block.end:
                return block
        return None

    def block_by_label(self, label: str) -> DocxBlock | None:
        """按"第几张表"找块（T01 / T02）。"""

        wanted = (label or "").strip().upper()
        if not wanted.startswith("T"):
            return None
        for block in self.blocks:
            if block.kind == "table" and block.label.upper() == wanted:
                return block
        return None

    def describe(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for block in self.blocks:
            kinds[block.kind] = kinds.get(block.kind, 0) + 1
        writable = sum(1 for block in self.blocks if block.pieces)
        return {
            "blocks": len(self.blocks),
            "kinds": kinds,
            "writable_blocks": writable,
            "with_formula": sum(1 for block in self.blocks if "math" in block.flags),
            "with_object": sum(1 for block in self.blocks if "object" in block.flags),
            "chars": len(self.text),
            "warnings": list(self.warnings),
        }

    # ---------- 写 ----------

    def flush(self) -> None:
        """把内存里改过的 document.xml 写回 parts。"""

        self.parts["word/document.xml"] = self.etree.tostring(
            self.document_root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )

    def save(self, path: str | Any) -> None:
        """另存为一个新的 .docx（原文件不动）。"""

        self.flush()
        target = str(path)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in self.parts.items():
                archive.writestr(name, data)
