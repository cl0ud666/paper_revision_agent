"""
把"已接受的改动"写回 Word 原文件：**Word 原生修订痕迹 + 批注**。

这是整条链路里最后、也是用户唯一真正打开的东西。
它的原则只有一条：**不确定就不改，并把"为什么没改"留成批注让用户自己决定**。

能写回的三种情况：

1. **整段重写**：范围覆盖整段的全部内容（含公式、图片、换行）时，
   把这一段原有的内容整体包进 ``<w:del>``，再用 ``<w:ins>`` 插入新文字；
2. **句内替换**：范围落在一段连续的普通文字里时，按 run 切开，
   只把被替换的那几个字标成 ``<w:del>`` / ``<w:ins>``；
3. **表格单元格内替换**：只要改动不跨单元格，处理和普通段落一样。

写不回去的情况（写回层**拒绝**，而不是猜）：

- 改动跨了多个段落或单元格（Markdown 表格里的竖线在 Word 里不存在）；
- 改动范围里有图片、公式、换行、制表符，而它不是整段重写；
- 原文与补丁里记的"修改前"对不上（说明文档在中间被别的程序动过）。

每条改动都带一条 Word 批注写明理由；写不进去的改动也会在原地留批注，
这样用户不必回去翻报告就知道"还有哪一条要自己动手"。

关于"重放"：回写不是把最后一次的文字翻译成 Word，而是
**原文件 + 第一轮改动 + 第二轮改动……** 逐轮落实。
每一条补丁在生成时都已经过三重校验（段落 ID + 句子序号 + 原文引用），
所以重放出来的文字与当时的文档逐字一致。
"""

from __future__ import annotations

import copy
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.document.docx_model import (
    CT_NS,
    PKG_REL_NS,
    W_NS,
    DocxBlock,
    DocxSource,
    Piece,
    w,
)
from app.document.spans import Span, build_spans, resolve_span

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

REL_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_SETTINGS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_SETTINGS = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"

DEFAULT_AUTHOR = "论文修改助手"


def _utc_now() -> str:
    """Word 习惯的 ISO 时间（带 Z 后缀）。"""

    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _int_or(value: Any, default: int) -> int:
    """把补丁里的数字读成 int（0 是合法值，不能被当成"没有"）。"""

    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 结果


@dataclass
class RedlineOutcome:
    """一次回写的结果。"""

    out_path: str = ""
    text: str = ""
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    changes: int = 0
    comments: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.applied)

    def summary(self) -> str:
        parts = [f"写回 {len(self.applied)} 条改动（Word 修订痕迹）"]
        if self.skipped:
            parts.append(f"{len(self.skipped)} 条需要人工处理")
        parts.append(f"附 {self.comments} 条批注")
        return "，".join(parts) + "。"

    def to_json(self) -> dict[str, Any]:
        return {
            "out_path": self.out_path,
            "applied": len(self.applied),
            "skipped": len(self.skipped),
            "changes": self.changes,
            "comments": self.comments,
            "warnings": list(self.warnings),
            "skipped_items": [
                {
                    "patch_id": item.get("patch_id"),
                    "position": item.get("position") or item.get("span_label") or "",
                    "location": item.get("location") or "",
                    "before": item.get("before") or item.get("quote") or "",
                    "after": item.get("replace") or "",
                    "rationale": item.get("rationale") or "",
                    "reason": item.get("reason"),
                }
                for item in self.skipped
            ],
        }


# ---------------------------------------------------------------- 一条改动的落点


@dataclass
class Change:
    """一条补丁在文档里的确切落点。"""

    patch: dict[str, Any]
    block: DocxBlock
    paragraph: Any
    pieces: list[Piece]
    full: bool
    start: int  # 相对所属块文本
    end: int

    @property
    def first(self) -> Piece:
        return self.pieces[0]

    @property
    def last(self) -> Piece:
        return self.pieces[-1]


class Unlocatable(ValueError):
    """这条改动无法安全落到 XML 上（原因写给用户看）。"""


def _locate(
    patch: dict[str, Any],
    *,
    source: DocxSource,
    spans: list[Span],
    text: str,
) -> Change:
    """
    把一条补丁翻译成"改哪几个 XML 节点、从哪到哪"。

    这里**不做任何猜测**：任何一个条件不满足就直接说原因。
    宁可让用户手工改一个字，也不要把论文改错。
    """

    span = resolve_span(
        spans,
        span_id=str(patch.get("span_id") or ""),
        label=str(patch.get("span_label") or ""),
    )
    if span is None:
        raise Unlocatable("找不到目标段落")

    start_in = _int_or(patch.get("start_in_span"), -1)
    end_in = _int_or(patch.get("end_in_span"), -1)
    if start_in < 0 or end_in < start_in:
        raise Unlocatable("这条改动没有可用的定位信息")

    block = source.block_at(span.start)
    if block is None:
        raise Unlocatable("目标位置不在可编辑的段落里")
    start = span.start + start_in
    end = span.start + end_in

    before = str(patch.get("before") or "")
    if before and text[start:end] != before:
        raise Unlocatable("原文与补丁记录的“修改前”不一致，已在原地留批注")

    local_start = start - block.start
    local_end = end - block.start
    # 改动范围必须完整落在同一个块里（Markdown 装饰、块之间的空行都不算内容）
    if local_start < 0 or local_end > len(block.text):
        raise Unlocatable("这条改动跨了文档结构，需要人工处理")

    pieces = block.pieces_in(local_start, local_end)
    if not pieces:
        raise Unlocatable("改动落在排版标记上（不是正文文字），无法写回")
    if local_end <= local_start:
        raise Unlocatable("这条改动没有指定要替换的文字")

    if block.obstacles_in(local_start, local_end):
        raise Unlocatable("改动范围里有图片/图形对象，需要人工处理")

    owners = {id(piece.owner) for piece in pieces}
    if len(owners) > 1:
        raise Unlocatable("这条改动跨了多个段落或表格单元格，需要人工处理")

    paragraph = pieces[0].owner
    group = [piece for piece in block.pieces if piece.owner is paragraph]
    # "整段重写"只在段落里没有图片之类的对象时才成立：
    # 否则宁可当成句内替换，把图片原样留在那里。
    full = (
        local_start <= group[0].start
        and local_end >= group[-1].end
        and not block.obstacles
    )

    if not full:
        for piece in pieces:
            if piece.kind != "text" or piece.run is None:
                raise Unlocatable(
                    "改动范围里有公式/换行/制表符，只能整段重写，需要人工确认"
                )
        if len({id(piece.run) for piece in pieces}) != len(pieces):
            raise Unlocatable("这一处文字的结构特殊（一段 run 里有多段文字），需要人工处理")

    return Change(
        patch=patch,
        block=block,
        paragraph=paragraph,
        pieces=pieces,
        full=full,
        start=local_start,
        end=local_end,
    )


# ---------------------------------------------------------------- 回写器


class RedlineWriter:
    """在 :class:`DocxSource` 上生成修订痕迹与批注。"""

    def __init__(
        self,
        source: DocxSource,
        *,
        author: str = DEFAULT_AUTHOR,
        date: str | None = None,
        comments: bool = True,
    ) -> None:
        self.source = source
        self.etree = source.etree
        self.author = author
        self.date = date or _utc_now()
        self.comments_enabled = comments
        self._next_id = self._scan_ids()
        self._comments_root: Any = None
        self.changes = 0
        self.comments = 0
        self._ensure_settings()

    # ---------- 编号与基础部件 ----------

    def _scan_ids(self) -> int:
        """修订/批注的 w:id 不能与文档里已有的撞号。"""

        highest = 0
        roots = [self.source.document_root]
        raw_comments = self.source.parts.get("word/comments.xml")
        if raw_comments:
            try:
                roots.append(self.etree.fromstring(raw_comments))
            except Exception:  # noqa: BLE001 - 原批注坏了不该拖垮写回
                pass
        for root in roots:
            highest = max(highest, self._max_id(root))
        return highest + 1

    @staticmethod
    def _max_id(root: Any) -> int:
        highest = 0
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            value = element.get(w("id"))
            if value is None:
                continue
            try:
                highest = max(highest, int(value))
            except (TypeError, ValueError):
                continue
        return highest

    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _stamp(self, element: Any) -> Any:
        element.set(w("id"), str(self._id()))
        element.set(w("author"), self.author)
        element.set(w("date"), self.date)
        return element

    def _ensure_settings(self) -> None:
        """打开文档的"修订模式"，这样用户在 Word 里能直接看到痕迹。"""

        etree = self.etree
        name = "word/settings.xml"
        raw = self.source.parts.get(name)
        if raw:
            root = etree.fromstring(raw)
        else:
            root = etree.Element(w("settings"), nsmap={"w": W_NS})
            self._add_relationship("settings.xml", REL_SETTINGS)
            self._add_content_type("/word/settings.xml", CT_SETTINGS)

        if root.find(w("trackRevisions")) is None:
            root.insert(0, etree.Element(w("trackRevisions")))
        self.source.parts[name] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    def _rels_path(self) -> str:
        return "word/_rels/document.xml.rels"

    def _add_relationship(self, target: str, rel_type: str) -> None:
        """确保 document.xml 与某个部件之间有关系（否则 Word 会认为文件损坏）。"""

        etree = self.etree
        raw = self.source.parts.get(self._rels_path())
        root = (
            etree.fromstring(raw)
            if raw
            else etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
        )
        if any(rel.get("Type") == rel_type for rel in root):
            return
        used = {rel.get("Id", "") for rel in root}
        index = 1
        while f"rId{index}" in used:
            index += 1
        rel = etree.SubElement(root, f"{{{PKG_REL_NS}}}Relationship")
        rel.set("Id", f"rId{index}")
        rel.set("Type", rel_type)
        rel.set("Target", target)
        self.source.parts[self._rels_path()] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    def _add_content_type(self, part_name: str, content_type: str) -> None:
        etree = self.etree
        name = "[Content_Types].xml"
        raw = self.source.parts.get(name)
        root = (
            etree.fromstring(raw)
            if raw
            else etree.Element(f"{{{CT_NS}}}Types", nsmap={None: CT_NS})
        )
        if any(
            override.get("PartName") == part_name
            for override in root.findall(f"{{{CT_NS}}}Override")
        ):
            return
        override = etree.SubElement(root, f"{{{CT_NS}}}Override")
        override.set("PartName", part_name)
        override.set("ContentType", content_type)
        self.source.parts[name] = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # ---------- 批注 ----------

    def _ensure_comments_part(self) -> Any:
        etree = self.etree
        name = "word/comments.xml"
        raw = self.source.parts.get(name)
        if raw:
            return etree.fromstring(raw)
        self._add_relationship("comments.xml", REL_COMMENTS)
        self._add_content_type("/word/comments.xml", CT_COMMENTS)
        return etree.Element(w("comments"), nsmap={"w": W_NS})

    def _store_comments(self) -> None:
        if self._comments_root is None:
            return
        self.source.parts["word/comments.xml"] = self.etree.tostring(
            self._comments_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    def _comment_id(self, text: str) -> int:
        if self._comments_root is None:
            self._comments_root = self._ensure_comments_part()
        comment_id = self._id()
        comment = self.etree.SubElement(self._comments_root, w("comment"))
        comment.set(w("id"), str(comment_id))
        comment.set(w("author"), self.author)
        comment.set(w("date"), self.date)
        paragraph = self.etree.SubElement(comment, w("p"))
        for position, line in enumerate(str(text).split("\n")):
            run = self.etree.SubElement(paragraph, w("r"))
            if position:
                self.etree.SubElement(run, w("br"))
            node = self.etree.SubElement(run, w("t"))
            node.set(XML_SPACE, "preserve")
            node.text = line
        self._store_comments()
        self.comments += 1
        return comment_id

    def _anchor_comment(self, parent: Any, nodes: list[Any], comment_id: int) -> None:
        """在 nodes 外面套上批注锚点（Word 认这三件套）。"""

        if not nodes:
            return
        etree = self.etree

        start = etree.Element(w("commentRangeStart"))
        start.set(w("id"), str(comment_id))
        parent.insert(parent.index(nodes[0]), start)

        end = etree.Element(w("commentRangeEnd"))
        end.set(w("id"), str(comment_id))
        parent.insert(parent.index(nodes[-1]) + 1, end)

        reference = etree.Element(w("r"))
        marker = etree.SubElement(reference, w("commentReference"))
        marker.set(w("id"), str(comment_id))
        parent.insert(parent.index(end) + 1, reference)

    def note_on_paragraph(self, paragraph: Any, text: str) -> None:
        """在某个段落上留一条"需要人工处理"的批注（不改文字）。

        批注要能直接当"待办"用：写清**改哪里、原来是什么、建议改成什么、为什么没自动改**，
        否则用户看到"需要人工处理"也不知道该动哪里。
        """

        if not self.comments_enabled or paragraph is None:
            return
        # 锚点必须和批注范围是同一个父节点，否则 Word 认为锚点无效
        anchor = next((child for child in paragraph if child.tag == w("r")), None)
        if anchor is None:
            anchor = next((child for child in paragraph if child.tag != w("pPr")), None)
        if anchor is None:
            return
        self._anchor_comment(paragraph, [anchor], self._comment_id(text))

    # ---------- 修订痕迹 ----------

    def _plain_run(self, template: Any, text: str, *, deleted: bool = False) -> Any:
        """用原 run 的格式（rPr）造一个新 run。"""

        etree = self.etree
        run = etree.Element(w("r"))
        if template is not None:
            properties = template.find(w("rPr"))
            if properties is not None:
                run.append(copy.deepcopy(properties))
        node = etree.SubElement(run, w("delText") if deleted else w("t"))
        if text != text.strip() or not text:
            node.set(XML_SPACE, "preserve")
        node.text = text
        return run

    def _mark_deleted(self, node: Any) -> None:
        """把既有内容整体标成"已删除"：w:t -> w:delText，已插入的内容拆掉包装。"""

        for insertion in list(node.iter(w("ins"))):
            if insertion is node:
                continue
            parent = insertion.getparent()
            if parent is None:
                continue
            index = parent.index(insertion)
            for grandchild in list(insertion):
                insertion.remove(grandchild)
                parent.insert(index, grandchild)
                index += 1
            parent.remove(insertion)
        for text in node.iter(w("t")):
            text.tag = w("delText")

    def _inside_insertion(self, run: Any, paragraph: Any) -> bool:
        """这个 run 是不是本工具（或原作者）之前插入的内容。"""

        node = run.getparent()
        while node is not None and node is not paragraph:
            if node.tag == w("ins"):
                return True
            node = node.getparent()
        return False

    def _replace(self, change: Change) -> None:
        """真正动手：把 change.start..change.end 换成新文字，并留下痕迹与批注。"""

        etree = self.etree
        paragraph = change.paragraph
        patch = change.patch
        new_text = str(patch.get("replace") or "")
        delete = bool(patch.get("delete"))

        nodes: list[Any] = []

        if change.full:
            keep = {w("pPr"), w("sectPr")}
            affected = [child for child in list(paragraph) if child.tag not in keep]
            if not affected:
                raise Unlocatable("目标段落没有内容")
            parent = paragraph
            index = paragraph.index(affected[0])
            deletion = etree.Element(w("del"))
            self._stamp(deletion)
            for child in affected:
                paragraph.remove(child)
                deletion.append(child)  # 移动而不是复制，避免书签/批注锚点重复
            self._mark_deleted(deletion)
            paragraph.insert(index, deletion)
            nodes.append(deletion)
            template = next((child for child in deletion.iter(w("r"))), None)

            if new_text and not delete:
                insertion = etree.Element(w("ins"))
                self._stamp(insertion)
                insertion.append(self._plain_run(template, new_text))
                parent.insert(index + 1, insertion)
                nodes.append(insertion)
        else:
            runs = [piece.run for piece in change.pieces]
            parent = runs[0].getparent()
            if parent is None or any(run.getparent() is not parent for run in runs):
                # 例如一段文字一半在超链接里一半在外面：这种结构不猜
                raise Unlocatable("这段文字分布在不同的结构里（如超链接），需要人工处理")
            index = parent.index(runs[0])
            inserted_before = self._inside_insertion(runs[0], paragraph)

            # 范围之外、还要留在原处的那两截文字
            prefix = self._slice(change.first, change.first.start, change.start)
            suffix = self._slice(change.last, change.end, change.last.end)
            deleted = []
            for piece in change.pieces:
                text = self._slice(piece, change.start, change.end)
                if text:
                    deleted.append(self._plain_run(piece.run, text, deleted=True))
            template = change.first.run
            for run in runs:
                parent.remove(run)

            nodes_to_insert: list[Any] = []
            if prefix:
                nodes_to_insert.append(self._plain_run(change.first.run, prefix))
            if inserted_before:
                # 这段文字本来就是上一轮插进来的：直接改，不再套一层"删除"
                if new_text and not delete:
                    nodes_to_insert.append(self._plain_run(template, new_text))
            else:
                if deleted:
                    deletion = etree.Element(w("del"))
                    self._stamp(deletion)
                    for run in deleted:
                        deletion.append(run)
                    nodes_to_insert.append(deletion)
                    nodes.append(deletion)
                if new_text and not delete:
                    insertion = etree.Element(w("ins"))
                    self._stamp(insertion)
                    insertion.append(self._plain_run(template, new_text))
                    nodes_to_insert.append(insertion)
                    nodes.append(insertion)
            if suffix:
                nodes_to_insert.append(self._plain_run(change.last.run, suffix))

            for node in reversed(nodes_to_insert):
                parent.insert(index, node)

        if self.comments_enabled and nodes:
            rationale = str(patch.get("rationale") or "").strip()
            patch_id = str(patch.get("patch_id") or "")
            note = f"{patch_id}：{rationale}" if rationale else patch_id
            self._anchor_comment(parent, nodes, self._comment_id(note))

        self.changes += 1

    @staticmethod
    def _slice(piece: Piece, start: int, end: int) -> str:
        """
        取一个片段里 [start, end) 对应的文字（相对所属块文本的偏移）。

        ``rendered`` 可能是 ``node.text`` 的一部分，所以这里既做块内裁剪，
        又用 ``node_offset`` 保证与原节点的下标一致。
        """

        left = max(start, piece.start)
        right = min(end, piece.end)
        if left >= right:
            return ""
        offset = piece.node_offset + (left - piece.start)
        node_text = piece.node.text or ""
        return node_text[offset : offset + (right - left)]


# ---------------------------------------------------------------- 顶层接口


def applied_rounds(state: Any) -> list[list[dict[str, Any]]]:
    """
    从 state 里取出"每一轮真正落实的改动"，按落实顺序。

    回写靠**重放**：原文件 + 第一轮改动 + 第二轮改动……
    """

    rounds: list[list[dict[str, Any]]] = []
    for entry in state.get("revision_history") or []:
        if not isinstance(entry, dict):
            continue
        applied = entry.get("applied")
        if entry.get("stage") == "apply" and applied:
            rounds.append([dict(item) for item in applied])
    return rounds


def _split_run(run: Any, cuts: dict[Any, set[int]]) -> None:
    """
    把一个 run 按给定位置切开，切成多个"只有一段文字"的 run。

    为什么必须做：Word 里一个段落常常只有一个 run（整段文字挤在一个节点里），
    同一段的多处改动如果都去动这一个 run，第一处就会把后面几处的目标节点拆掉。
    先按改动边界切开，每一处改动就有了自己的 run，互不影响。

    切 run **不改变文字**，所以拆完之后字符偏移依然有效。
    """

    parent = run.getparent()
    if parent is None:
        return
    index = parent.index(run)
    properties = run.find(w("rPr"))

    segments: list[list[Any]] = [[]]
    for child in list(run):
        if child.tag == w("rPr"):
            continue
        if child.tag in {w("t"), w("delText")}:
            text = child.text or ""
            offsets = sorted(
                offset for offset in cuts.get(child, set()) if 0 < offset < len(text)
            )
            position = 0
            for offset in offsets + [len(text)]:
                piece_text = text[position:offset]
                clone = copy.deepcopy(child)
                clone.text = piece_text
                if piece_text != piece_text.strip() or not piece_text:
                    clone.set(XML_SPACE, "preserve")
                segments[-1].append(clone)
                if offset != len(text):
                    segments.append([])
                position = offset
            continue
        segments[-1].append(copy.deepcopy(child))

    if len(segments) == 1:
        return

    rebuilt: list[Any] = []
    for segment in segments:
        if not segment:
            continue
        new_run = _new_element(parent, w("r"))
        if properties is not None:
            new_run.append(copy.deepcopy(properties))
        for child in segment:
            new_run.append(child)
        rebuilt.append(new_run)

    for position, new_run in enumerate(rebuilt):
        parent.insert(index + position, new_run)
    parent.remove(run)


def _new_element(parent: Any, tag: str) -> Any:
    """在 parent 所在的文档树上新建一个元素（沿用同一套命名空间）。"""

    return parent.makeelement(tag, {})


def _split_for_changes(source: DocxSource, changes: list[Change]) -> None:
    """
    按本轮所有改动的边界，把涉及到的 run 先切开。

    同一段里有多处改动时，这一步是必须的：否则第一处改动会把
    后面几处的目标节点一起换掉，后面几处就只能报"写回失败"。
    """

    groups: dict[int, tuple[Any, list[Change]]] = {}
    for change in changes:
        entry = groups.get(id(change.paragraph))
        if entry is None:
            groups[id(change.paragraph)] = (change.paragraph, [change])
        else:
            entry[1].append(change)

    for _paragraph, group in groups.values():
        if all(change.full for change in group):
            continue
        # 文本节点 -> 需要切开的"节点内偏移"
        node_cuts: dict[Any, set[int]] = {}
        for change in group:
            if change.full:
                continue
            for piece in change.pieces:
                if piece.kind != "text" or piece.node is None:
                    continue
                for bound in (change.start, change.end):
                    if piece.start < bound < piece.end:
                        node_cuts.setdefault(piece.node, set()).add(
                            piece.node_offset + (bound - piece.start)
                        )

        runs: dict[int, tuple[Any, dict[Any, set[int]]]] = {}
        for node, offsets in node_cuts.items():
            run = node.getparent()
            if run is None:
                continue
            entry = runs.get(id(run))
            if entry is None:
                runs[id(run)] = (run, {node: set(offsets)})
            else:
                entry[1].setdefault(node, set()).update(offsets)

        for run, cuts in runs.values():
            _split_run(run, cuts)


def export_redline(
    source_data: bytes,
    rounds: Iterable[Iterable[dict[str, Any]]],
    out_path: str,
    *,
    author: str = DEFAULT_AUTHOR,
    date: str | None = None,
    comments: bool = True,
) -> RedlineOutcome:
    """
    把若干轮的改动写回 .docx 副本。

    :param source_data: **原始** .docx 的字节（不是改过的文字）
    :param rounds: 每轮"已落实"的补丁（``Patch.to_json()`` 的形状）
    :param out_path: 输出路径（另存副本，原文件不动）
    """

    outcome = RedlineOutcome(out_path=out_path)
    source = DocxSource(source_data)
    writer = RedlineWriter(source, author=author, date=date, comments=comments)
    outcome.warnings.extend(source.warnings)

    text = source.text
    version = 1
    for items in rounds:
        spans = build_spans(text, version=version)
        prepared: list[Change] = []
        for item in items:
            try:
                change = _locate(dict(item), source=source, spans=spans, text=text)
            except Unlocatable as exc:
                outcome.skipped.append({**item, "reason": str(exc)})
                if comments:
                    span = resolve_span(
                        spans,
                        span_id=str(item.get("span_id") or ""),
                        label=str(item.get("span_label") or ""),
                    )
                    writer.note_on_paragraph(
                        _anchor_paragraph(source, span),
                        _manual_todo(item, str(exc), span),
                    )
                continue
            prepared.append(change)

        # 先把 run 按改动边界切开：同一段里的多处改动才不会互相拆掉目标
        _split_for_changes(source, prepared)
        source.rebuild()

        # 拆 run 不改变文字，所以偏移还有效；这里是按新的结构重新取一遍锚点
        ready: list[Change] = []
        for change in prepared:
            block = source.blocks[change.block.index]
            pieces = block.pieces_in(change.start, change.end)
            if not pieces:
                outcome.skipped.append(
                    {**change.patch, "reason": "切分文字后无法重新定位，需要人工处理"}
                )
                continue
            if len({id(piece.owner) for piece in pieces}) > 1:
                outcome.skipped.append(
                    {**change.patch, "reason": "这条改动跨了多个段落或表格单元格，需要人工处理"}
                )
                continue
            if not change.full and any(piece.kind != "text" for piece in pieces):
                outcome.skipped.append(
                    {
                        **change.patch,
                        "reason": "改动范围里有公式/换行/制表符，只能整段重写，需要人工确认",
                    }
                )
                continue
            ready.append(
                Change(
                    patch=change.patch,
                    block=block,
                    paragraph=change.paragraph,
                    pieces=pieces,
                    full=change.full,
                    start=change.start,
                    end=change.end,
                )
            )

        # 从后往前改：前面的偏移不会被后面的改动打乱
        for change in sorted(ready, key=lambda item: item.start + item.block.start, reverse=True):
            try:
                writer._replace(change)
            except Exception as exc:  # noqa: BLE001 - 单条失败不该毁掉整篇
                outcome.skipped.append({**change.patch, "reason": f"写回失败：{exc}"})
                continue
            outcome.applied.append(dict(change.patch))

        source.rebuild()
        text = source.text
        version += 1

    writer._store_comments()
    source.save(out_path)
    outcome.text = source.text
    outcome.changes = writer.changes
    outcome.comments = writer.comments
    return outcome


def _manual_todo(item: dict[str, Any], reason: str, span: Span | None) -> str:
    """
    写不回的改动 → 一条能直接照着做的"待办"批注。

    "这条改动没有写回 Word" 这种话对用户没有用；
    有用的是：在哪一段、原文是什么、建议改成什么、为什么没自动改。
    """

    lines = ["【需人工处理】这条改动没能自动写回 Word。"]
    if span is not None:
        where = span.label
        if span.section and span.section != span.text:
            where += f"（{span.section}）"
        lines.append(f"位置：{where}")
    location = str(item.get("location") or "").strip()
    if location and location not in lines[-1]:
        lines.append(f"章节：{location}")
    before = str(item.get("before") or item.get("quote") or "").strip()
    if before:
        lines.append(f"原文：{before[:160]}")
    after = str(item.get("replace") or "").strip()
    if after:
        lines.append(f"建议改为：{after[:160]}")
    elif item.get("delete"):
        lines.append("建议：删除这段内容")
    lines.append(f"原因：{reason}")
    return "\n".join(lines)


def _anchor_paragraph(source: DocxSource, span: Span | None) -> Any | None:
    """给写不进去的改动找一个"挂批注"的段落。"""

    if span is None:
        return None
    block = source.block_at(span.start)
    if block is None:
        return None
    if block.kind == "table":
        return block.element.find(f".//{w('p')}")
    return block.element
