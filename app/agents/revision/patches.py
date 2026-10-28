"""Deterministic terminology edits and patch filtering."""

from __future__ import annotations

import re
from typing import Any, Callable

from app.document.patches import (
    STATUS_REJECTED,
    Patch,
    summarize_patches,
)
from app.document.spans import (
    Span,
)
from app.utils.consistency import find_naming_variants
from app.utils.text import (
    looks_like_code,
    normalize_whitespace,
    truncate_middle,
)
from app.utils.tracing import utc_now

_UNIFY_RE = re.compile(r"全文统一为\s*[「\"'“]?([^\s」\"'”，,。；;]+)")


def _unification_targets(rules: Any) -> list[str]:
    """从用户要求里取出「全文统一为X」里的目标写法X。"""

    targets: list[str] = []
    for rule in rules or []:
        if rule.get("kind") != "rule" or rule.get("status", "active") != "active":
            continue
        for match in _UNIFY_RE.finditer(str(rule.get("text") or "")):
            token = match.group(1).strip()
            if token and token not in targets:
                targets.append(token)
    return targets


def _unique_quote(text: str, start: int, end: int, token: str) -> str:
    """
    给某一次出现造一个"在段落里唯一"的引用片段。

    补丁要求quote在段落内唯一，否则会被判"出现多次、无法判断改哪一处"。
    同一个词在一段里出现两次时，就向两侧扩窗，直到这个片段唯一、
    且**只包含这一处**该词为止。
    """

    length = len(text)
    for pad in (0, 4, 8, 12, 20, 30, 50, 80, 120):
        left = max(0, start - pad)
        right = min(length, end + pad)
        piece = text[left:right]
        if text.count(piece) == 1 and piece.count(token) == 1:
            return piece
    return text


def _unification_patches(spans: list[Span], rules: Any) -> list[Patch]:
    """
    用户定了「全文统一为X」之后，**由程序**把全文替换一遍。

    为什么不交给模型：

    - 位置要**穷尽**（这篇论文里MoCo有40+ 处），模型在长文里列不全；
    - 词级替换的正确性**完全可判定**（就是字符串替换），不需要模型判断；
    - 模型会连代码里的常量名一起改（`MOCO_QUEUE_SIZE`），而**代码一律不碰**。

    所以：口径来自用户在闸门上的选择（agent的判断），
    执行由程序完成（穷尽、确定、零成本）。
    """

    targets = _unification_targets(rules)
    if not targets:
        return []

    # 代码段一律不参与：常量名/路径里的大小写不归本文档管
    prose = [span for span in spans if not looks_like_code(span.text)]
    by_label = {span.label: span for span in prose}

    patches: list[Patch] = []
    for item in find_naming_variants(prose):
        variants = item.get("variants") or {}
        target = next((token for token in targets if token in variants), None)
        if target is None:
            continue
        for token, labels in variants.items():
            if token == target:
                continue
            for label in labels:
                span = by_label.get(label)
                if span is None:
                    continue
                for match in re.finditer(re.escape(token), span.text):
                    quote = _unique_quote(span.text, match.start(), match.end(), token)
                    replaced = quote.replace(token, target)
                    if replaced == quote:
                        continue
                    patches.append(
                        Patch(
                            patch_id="",
                            span_id=span.id,
                            span_label=span.label,
                            target="sentence",
                            quote=quote,
                            replace=replaced,
                            rationale=(
                                f"按用户要求，全文统一为「{target}」；"
                                f"此处「{token}」改为「{target}」"
                            ),
                            category="专有名词",
                            severity="medium",
                            strategy="unification",
                        )
                    )
    return patches


def _drop_noop_patches(
    patches: list[Patch],
    notes: list[str],
    log: Callable[[str], None],
) -> list[Patch]:
    """
    丢掉"修改前后一模一样"的补丁。

    模型有时会提一条replace和quote逐字相同的补丁——等于什么都没改。
    它既不该出现在用户的清单里（清单里全是"要改什么"），
    更不该被显示成"**无法定位**"：那三个字会让人以为定位失败，
    而实际上压根没有改动可定位。这类补丁直接丢掉，只在日志里留一句计数。
    """

    kept = [patch for patch in patches if patch.conflict_code != "no_change"]
    dropped = len(patches) - len(kept)
    if dropped:
        note = f"已跳过 {dropped} 条「修改前后相同」的无效补丁"
        notes.append(note)
        log(f"[Revision] {note}")
    return kept


def _drop_code_patches(
    patches: list[Patch],
    spans: list[Span],
    notes: list[str],
    log: Callable[[str], None],
) -> list[Patch]:
    """
    丢掉所有落在**代码段**上的补丁。

    论文里的代码是"印刷出来的清单"，工具读到的是抽取文本：上下文往往不全
    （例如模块级常量根本没写进论文），它没有能力判断代码对不对。
    让它去"顺手修bug"，结果是拿猜测去改正确的代码——
    而且代码段里有换行，改回Word也写不进去。所以代码一律不动。
    """

    by_id = {span.id: span for span in spans}
    by_label = {span.label: span for span in spans}

    kept: list[Patch] = []
    dropped_labels: set[str] = set()
    for patch in patches:
        span = by_id.get(getattr(patch, "span_id", "") or "") or by_label.get(
            getattr(patch, "span_label", "") or ""
        )
        if patch.conflict_code == "code_protected" or (
            span is not None and looks_like_code(span.text)
        ):
            if span is not None and span.label:
                dropped_labels.add(span.label)
            continue
        kept.append(patch)

    dropped = len(patches) - len(kept)
    if dropped:
        where = "、".join(sorted(dropped_labels)) or "（未定位）"
        note = f"已跳过 {dropped} 条涉及代码段的改动（代码不由本工具修改）：{where}"
        notes.append(note)
        log(f"[Revision] {note}")
    return kept


def _mark_previously_rejected(patches: list[Patch], state: Any) -> list[Patch]:
    """
    与“已被拒绝的改动”比对：完全一样的改动直接标成已拒绝。

    这样用户不会被同一个方案反复打扰（多轮会话里的体验关键）。
    """

    memory = {
        (item.get("span_id", ""), normalize_whitespace(item.get("replace", "")))
        for item in (state.get("rejected_memory") or [])
    }
    if not memory:
        return patches

    for patch in patches:
        key = (patch.span_id, normalize_whitespace(patch.replace))
        if key in memory and patch.status == "proposed":
            patch.status = STATUS_REJECTED
            patch.decided_by = "system"
            patch.decision_note = "与之前被用户拒绝的改动相同，已自动跳过"
            patch.decided_at = utc_now()
    return patches


def _build_summary(
    targets: list[dict[str, Any]],
    patches: list[Patch],
    notes: list[str],
    failed_batches: int,
    budget_stopped: bool,
) -> str:
    """给Review与报告使用的摘要。"""

    stats = summarize_patches(patches)
    lines = [
        f"针对 {len(targets)} 个问题提出 {len(patches)} 条补丁："
        f"可接受 {stats.get('proposed', 0)} 条、"
        f"无法定位 {stats.get('conflict', 0)} 条、"
        f"历史已拒绝 {stats.get('rejected', 0)} 条。"
    ]
    for patch in patches[:6]:
        lines.append(f"- {patch.patch_id} {patch.summary}")
    if len(patches) > 6:
        lines.append(f"- ……（其余 {len(patches) - 6} 条见补丁清单）")
    if failed_batches:
        lines.append(f"- {failed_batches} 批补丁提出失败（已跳过）")
    if budget_stopped:
        lines.append("- 预算不足，未处理完全部问题")
    if notes:
        lines.append("- 模型说明：" + "；".join(truncate_middle(note, 120) for note in notes[:3]))
    return "\n".join(lines)
