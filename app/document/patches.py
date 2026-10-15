"""
补丁（Patch）：Revision 的输出单位，也是用户逐条接受 / 拒绝的对象。

一条补丁回答四个问题：

1. **改哪里**：``span_id``（段落稳定 ID）+ 可选 ``quote``（段落内要替换的确切文字）
   + 可选句子序号 -> 落实到句级 ``span_id:S2``；
2. **改成什么**：``replace``；
3. **为什么改**：``rationale``（由 Agent 写，用户不需要写）；
4. **这条算不算数**：``status``（proposed / accepted / rejected / applied / conflict）。

校验规则（``validate_patch``）刻意做得严格：先在**目标段落内部**定位，
所以在全文中重复出现的句子不会造成歧义；找不到或有多义时记为 ``conflict``
并给出原因，而不是硬改原文。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from app.document.spans import Span, build_spans, resolve_span, sentence_id
from app.utils.edits import locate_unique
from app.utils.text import compact_text, looks_like_code, normalize_whitespace
from app.utils.tracing import utc_now

STATUS_PROPOSED = "proposed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_CONFLICT = "conflict"

STATUS_LABELS = {
    STATUS_PROPOSED: "待决定",
    STATUS_ACCEPTED: "已接受",
    STATUS_REJECTED: "已拒绝",
    STATUS_APPLIED: "已应用",
    STATUS_CONFLICT: "无法定位",
}

CONFLICT_TEXT = {
    "empty": "该段落中找不到要替换的原文片段",
    "not_found": "该段落中找不到要替换的原文片段",
    "ambiguous": "要替换的片段在该段落中出现多次，无法判断改哪一处",
    "no_span": "找不到目标段落",
    "empty_replace": "修改后的内容为空（如果要删除内容，请把 delete 设为 true）",
    "no_change": "修改前后内容相同（无效改动）",
    "overlap": "与同一段落的另一条补丁重叠",
    "code_protected": "代码内容或变量名受到保护，不会由工具修改",
}

# 被拒补丁的**短标签**。
#
# 以前这些一律显示成"无法定位"，但其中一半根本不是定位问题：
# "改后为空""前后相同"是**改动本身无效**，"与别的补丁重叠"是**两条改动打架**。
# 一律叫"无法定位"会让用户以为系统找不到位置，其实位置找得好好的。
CONFLICT_LABELS = {
    "empty": "无法定位",
    "not_found": "无法定位",
    "no_span": "无法定位",
    "ambiguous": "定位有歧义",
    "empty_replace": "无效改动",
    "no_change": "无效改动",
    "overlap": "改动冲突",
    "code_protected": "代码不修改",
}

_CODE_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_]\w*_[A-Za-z_]\w*(?![A-Za-z0-9_])")


def changes_code(before: str, after: str, span_text: str) -> bool:
    """代码段不修改；正文中引用的 snake_case/常量名也必须逐字保留。"""
    return looks_like_code(span_text) or Counter(_CODE_IDENTIFIER_RE.findall(before)) != Counter(
        _CODE_IDENTIFIER_RE.findall(after)
    )


@dataclass
class Patch:
    """一条可独立接受 / 拒绝的修改。"""

    patch_id: str
    span_id: str
    span_label: str = ""
    issue_id: Any = None
    target: str = "span"  # span / sentence
    quote: str = ""
    replace: str = ""
    rationale: str = ""
    delete: bool = False  # 删除类修改（replace 允许为空）
    category: str = ""
    severity: str = ""
    round: int = 1
    status: str = STATUS_PROPOSED
    conflict_reason: str = ""
    # 被拒的**原因代码**（not_found / ambiguous / no_change / overlap …）。
    # conflict_reason 是给人读的整句话，这个是机器可判的短码，
    # 短标签（"无法定位"/"定位有歧义"/"无效改动"/"改动冲突"）由它决定。
    conflict_code: str = ""
    warnings: list[str] = field(default_factory=list)
    strategy: str = ""
    sentence_index: int = 0
    start_in_span: int = -1
    end_in_span: int = -1
    before: str = ""
    decided_by: str = ""
    decision_note: str = ""
    created_at: str = ""
    decided_at: str = ""
    applied_at: str = ""
    span_id_after: str = ""

    # ---------- 展示辅助 ----------

    @property
    def sentence_ref(self) -> str:
        if self.sentence_index and self.span_id:
            return sentence_id(self.span_id, self.sentence_index)
        return ""

    @property
    def decidable(self) -> bool:
        """只有能定位的补丁才允许接受。"""

        return self.status in {STATUS_PROPOSED, STATUS_ACCEPTED}

    @property
    def position(self) -> str:
        if self.sentence_ref:
            return f"{self.span_label or self.span_id}（第 {self.sentence_index} 句）"
        return self.span_label or self.span_id

    @property
    def summary(self) -> str:
        before = compact_text(self.before or self.quote or "（整段）", 40)
        after = compact_text(self.replace, 40) if self.replace.strip() else "（删除）"
        return f"{self.position}：{before} → {after}"

    @property
    def status_label(self) -> str:
        """
        给用户看的状态词。

        被拒的补丁按**原因**细分：找不到原文/找不到段落才是"无法定位"，
        "改后为空""前后相同"是"无效改动"，"与别的补丁重叠"是"改动冲突"。
        """

        if self.status == STATUS_CONFLICT:
            return CONFLICT_LABELS.get(
                self.conflict_code, STATUS_LABELS[STATUS_CONFLICT]
            )
        return STATUS_LABELS.get(self.status, self.status)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status_label"] = self.status_label
        payload["position"] = self.position
        payload["sentence_ref"] = self.sentence_ref
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Patch":
        known = {key: value for key, value in payload.items() if key in cls.__dataclass_fields__}
        return cls(**known)


def next_patch_id(counter: int) -> str:
    return f"patch-{int(counter):03d}"


def _pick(item: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def patch_from_model(
    item: dict[str, Any],
    *,
    spans: Iterable[Span],
    patch_id: str,
    round_number: int = 1,
    issue: dict[str, Any] | None = None,
) -> Patch:
    """
    把模型返回的一条修改转成 Patch 对象（字段名容忍多种写法）。
    """

    spans = list(spans)
    span = resolve_span(
        spans,
        span_id=str(_pick(item, "span_id", "block_id", "id", "target_id", default="")).strip(),
        label=str(_pick(item, "span_label", "label", "paragraph", "location", default="")).strip(),
    )

    quote = str(_pick(item, "quote", "find", "original", "before", "target_text", default="") or "").strip()
    target = str(_pick(item, "target", "scope", default="span") or "span").strip().lower()

    raw_replace = _pick(item, "replace", "revised", "after", "new_text", default=None)
    if raw_replace is None and quote:
        # 有的模型会把修改后的文本写在 suggestion / new 字段里
        raw_replace = item.get("suggestion", item.get("new"))
    replace = str(raw_replace or "")

    patch = Patch(
        patch_id=patch_id,
        span_id=span.id if span else str(_pick(item, "span_id", "block_id", default="")).strip(),
        span_label=span.label if span else str(_pick(item, "span_label", "label", default="")).strip(),
        issue_id=_pick(item, "issue_id", "issue", default=None),
        target="sentence" if target.startswith("sent") or target.startswith("句") else "span",
        quote=quote,
        replace=replace,
        rationale=str(_pick(item, "rationale", "reason", "why", "explanation", default="") or "").strip(),
        category=str(_pick(item, "category", default=(issue or {}).get("category", "")) or ""),
        severity=str(_pick(item, "severity", default=(issue or {}).get("severity", "")) or ""),
        round=int(round_number),
        created_at=utc_now(),
        delete=bool(_pick(item, "delete", default=False))
        or str(_pick(item, "action", default="")).strip().lower() in {"delete", "remove", "删除"},
    )

    if patch.issue_id is None and issue is not None:
        patch.issue_id = issue.get("id")
    if not patch.rationale:
        patch.warnings.append("缺少修改理由")
    return patch


def validate_patch(patch: Patch, spans: Iterable[Span]) -> Patch:
    """
    在目标段落内部定位待替换文本，并给出可应用的位置。

    失败时把 patch.status 置为 conflict，并写明原因（前端会展示，用户也能看到
    “这条改不动”而不是悄悄丢掉）。
    """

    spans = list(spans)
    span = resolve_span(spans, span_id=patch.span_id, label=patch.span_label)
    if span is None:
        return _conflict(patch, "no_span")
    if looks_like_code(span.text):
        return _conflict(patch, "code_protected")

    patch.span_id = span.id
    patch.span_label = span.label

    # 排版标记（### / - / |）不是正文：模型抄进来的、自己加的，一律剥掉
    normalize_structure_markers(patch, span)

    if not (patch.replace or "").strip() and not patch.delete:
        return _conflict(patch, "empty_replace")

    # 模型有时会把我们用于定位的句子编号 "(1) (2)" 抄进修改结果里，
    # 这里做一次确定性清理，避免论文正文里出现奇怪的编号。
    cleaned = strip_sentence_markers(patch.replace)
    if cleaned != patch.replace:
        patch.replace = cleaned
        patch.warnings.append("已自动移除模型回抄的句子编号 (1)(2)")

    quote = (patch.quote or "").strip()
    if not quote:
        if not patch.delete and normalize_whitespace(span.text) == normalize_whitespace(patch.replace):
            return _conflict(patch, "no_change")
        if changes_code(span.text, patch.replace, span.text):
            return _conflict(patch, "code_protected")
        # 整段替换
        patch.strategy = "span-delete" if patch.delete else "span-whole"
        patch.start_in_span = 0
        patch.end_in_span = len(span.text)
        patch.before = span.text
        patch.status = STATUS_PROPOSED
        return patch

    if normalize_whitespace(quote) == normalize_whitespace(patch.replace):
        if not patch.delete:
            return _conflict(patch, "no_change")

    # 注意：这里用“严格唯一”定位——同一段落里出现多次时判定为歧义，
    # 而不是随便挑第一处（那是 find_span 的行为，不适合落盘修改）。
    start, end, strategy = locate_unique(span.text, quote)
    if start < 0:
        return _conflict(patch, strategy if strategy in CONFLICT_TEXT else "not_found")
    if changes_code(span.text[start:end], patch.replace, span.text):
        return _conflict(patch, "code_protected")

    patch.strategy = f"span-{strategy}"
    if patch.delete:
        patch.strategy = f"span-{strategy}-delete"
    patch.start_in_span = start
    patch.end_in_span = end
    patch.before = span.text[start:end]

    # 句级引用：记录它落在第几句（用户可以按句核对）
    offset = 0
    for index, sentence in enumerate(span.sentences(), start=1):
        if offset <= start < offset + len(sentence):
            patch.sentence_index = index
            break
        offset += len(sentence)

    if patch.target == "sentence" and not patch.sentence_index:
        patch.warnings.append("标注为句级修改，但未能确定句子位置")

    patch.status = STATUS_PROPOSED
    return patch


def _conflict(patch: Patch, reason: str) -> Patch:
    patch.status = STATUS_CONFLICT
    patch.conflict_code = reason
    patch.conflict_reason = CONFLICT_TEXT.get(reason, reason)
    return patch


_SENTENCE_MARKER_RE = re.compile(r"(?:^|(?<=[。！？；.!?;]))\s*\((\d{1,2})\)\s*")

# Markdown 结构标记：标题的 #、列表的 -、表格的 |
# 它们是**排版结构**，由文档结构保留，不属于正文内容。
_LEADING_MARKER_RE = re.compile(r"^(#{1,6}[ \t]+|[-*+][ \t]+)")


def _leading_marker(text: str) -> str:
    match = _LEADING_MARKER_RE.match(text or "")
    return match.group(0) if match else ""


def normalize_structure_markers(patch: Patch, span: Span) -> Patch:
    """
    把"排版标记"从补丁内容里剥掉。

    为什么必须做：送审文本里的 ``### 3.3.5 对比损失函数优化`` ，
    前面的 ``###`` 是**我们为了保留标题层级加的**，原文件里并不存在这段字符。
    模型很容易把它当成正文的一部分抄进 quote / replace，结果就是：

    - 标题变成 ``### ### 3.3.5 …``（标记重复）；
    - 或者补丁清单里显示"把 3.3.5 改成了 ### 3.3.5"，让用户以为正文多出了 ###；
    - 写回 Word 时，``###`` 会变成标题段落里真实的字符。

    处理方式：标记由结构保留，补丁只负责**文字本身**。剥掉之后记一条 warning，
    用户能在补丁清单里看到"已去掉模型回抄的排版标记"。
    """

    span_marker = _leading_marker(span.text)

    quote = patch.quote or ""
    if span_marker and quote.startswith(span_marker):
        patch.quote = quote[len(span_marker) :]
        patch.warnings.append("已去掉“修改前”回抄的排版标记")

    replace = patch.replace or ""
    if replace:
        marker = _leading_marker(replace)
        if marker:
            patch.replace = replace[len(marker) :]
            patch.warnings.append(
                "已去掉“修改后”开头的排版标记（标题层级由文档结构保留）"
            )
        replace = patch.replace

    if span.kind == "table" and replace:
        cleaned = replace.strip()
        if cleaned.startswith("|"):
            cleaned = cleaned[1:].lstrip()
        if cleaned.endswith("|"):
            cleaned = cleaned[:-1].rstrip()
        if cleaned != replace:
            patch.replace = cleaned
            patch.warnings.append("已去掉“修改后”两侧的表格竖线")

    return patch


def strip_sentence_markers(text: str) -> str:
    """
    去掉“给模型看的句子编号”。

    只有在编号成体系（至少两处、且从 1 开始连续递增）时才清理，
    以免误删作者真的写在正文里的 "（1）" 这类编号。
    """

    if not text:
        return text
    matches = list(_SENTENCE_MARKER_RE.finditer(text))
    if len(matches) < 2:
        return text
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(numbers) + 1)):
        return text

    cleaned = _SENTENCE_MARKER_RE.sub(lambda match: " " if match.start() > 0 else "", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def decide_patch(
    patch: Patch,
    decision: str,
    *,
    decided_by: str = "user",
    note: str = "",
) -> Patch:
    """接受或拒绝一条补丁（用户只需点一下，理由由 Agent 提供）。"""

    if patch.status == STATUS_CONFLICT and decision == "accept":
        if not patch.conflict_reason:
            patch.conflict_code = patch.conflict_code or "not_found"
            patch.conflict_reason = CONFLICT_TEXT["not_found"]
        return patch  # 无法定位的补丁不能被接受

    patch.status = STATUS_ACCEPTED if decision == "accept" else STATUS_REJECTED
    patch.decided_by = decided_by
    patch.decision_note = note
    patch.decided_at = utc_now()
    return patch


@dataclass
class ApplyOutcome:
    """一次“应用已接受补丁”的结果。"""

    document: str
    applied: list[Patch] = field(default_factory=list)
    skipped: list[Patch] = field(default_factory=list)
    spans_after: list[Span] = field(default_factory=list)


def apply_patches(
    document: str,
    spans: Iterable[Span],
    patches: Iterable[Patch],
    *,
    version_after: int | None = None,
) -> ApplyOutcome:
    """
    把所有“已接受”的补丁一次性应用到文档上。

    实现要点：

    1. 每条补丁先在**它自己的段落**里定位，再换算成全文偏移；
    2. 从后往前替换，避免前面的替换打乱后面的偏移；
    3. 互相重叠的补丁只保留靠前那条，另一条记为 conflict（提示用户）；
    4. 应用后重新索引文档，并回填 ``span_id_after``，把新旧 ID 连起来。
    """

    spans = list(spans)
    accepted = [patch for patch in patches if patch.status == STATUS_ACCEPTED]
    outcome = ApplyOutcome(document=document, spans_after=spans)
    if not accepted:
        return outcome

    targets: list[tuple[int, int, Patch]] = []
    for patch in accepted:
        span = resolve_span(spans, span_id=patch.span_id, label=patch.span_label)
        if span is None or patch.start_in_span < 0:
            patch.status = STATUS_CONFLICT
            if not patch.conflict_reason:
                patch.conflict_code = patch.conflict_code or "no_span"
                patch.conflict_reason = CONFLICT_TEXT["no_span"]
            outcome.skipped.append(patch)
            continue
        before = span.text[patch.start_in_span:patch.end_in_span]
        if changes_code(before, patch.replace, span.text):
            _conflict(patch, "code_protected")
            outcome.skipped.append(patch)
            continue
        start = span.start + patch.start_in_span
        end = span.start + patch.end_in_span
        targets.append((start, end, patch))

    targets.sort(key=lambda item: item[0])

    # 处理重叠：只保留更靠前的一条
    filtered: list[tuple[int, int, Patch]] = []
    for start, end, patch in targets:
        if filtered and start < filtered[-1][1]:
            patch.status = STATUS_CONFLICT
            patch.conflict_code = "overlap"
            patch.conflict_reason = CONFLICT_TEXT["overlap"]
            outcome.skipped.append(patch)
            continue
        filtered.append((start, end, patch))

    new_document = document
    applied: list[Patch] = []
    for start, end, patch in reversed(filtered):
        before = new_document[start:end]
        new_document = new_document[:start] + patch.replace + new_document[end:]
        patch.before = before
        patch.status = STATUS_APPLIED
        patch.applied_at = utc_now()
        applied.append(patch)

    version_after = version_after or (max((s.version for s in spans), default=1) + 1)
    spans_after = build_spans(new_document, version=version_after)

    # 回填新 ID（先归一化再比对，避免空白差异导致匹配失败）
    normalized_spans = [(span, normalize_whitespace(span.text)) for span in spans_after]
    for patch in applied:
        needle = normalize_whitespace(patch.replace)
        for span, text in normalized_spans:
            if needle and needle in text:
                patch.span_id_after = span.id
                break

    outcome.document = new_document
    # 删除类修改可能留下多余空行，顺手清理一下
    new_document = re.sub(r"\n{3,}", "\n\n", new_document)
    outcome.document = new_document
    outcome.applied = list(reversed(applied))
    outcome.spans_after = spans_after
    return outcome


def summarize_patches(patches: Iterable[Patch]) -> dict[str, int]:
    """按状态统计（报告与界面用）。"""

    counters = {status: 0 for status in STATUS_LABELS}
    for patch in patches:
        counters[patch.status] = counters.get(patch.status, 0) + 1
    return counters
