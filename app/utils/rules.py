"""
“用户要求与决定”的统一存储。

回答一个具体问题：**用户在对话里说的点，存在哪里？**

统一存在 State 的 ``user_rules`` 里（同时落盘到
``data/runs/<run_id>/state.json`` 与 session.json，并随 Checkpoint 一起持久化）。
每条是一条结构化记录：

    {
      "id": "R1",                  # 稳定编号，用户可以按编号修改/撤销
      "kind": "rule",              # rule = 用户定的规矩；ignore = 用户明确不改的问题
      "text": "全文统一把「土豆」写成「马铃薯」",
      "issue_ids": [1, 3],         # kind=ignore 时：被忽略的问题编号
      "signature": {...},          # kind=ignore 时的指纹（跨轮次识别同一问题）
      "created_turn": 1,
      "updated_turn": 1,
      "status": "active",          # active / revoked
      "source": "start",           # start / issue_review / patch_review / follow_up
      "created_at": "...",
      "updated_at": "..."
    }

为什么要编号：用户随时可能改主意（“把 R1 改成……”“取消 R1”），
没有编号就没法精确修改一个点，只能重新说一遍。

为什么 ignore 要存指纹：下一轮重新审查时问题编号会变，
只有指纹（位置 + 问题摘要）才能认出“这个问题用户上次说过不用管”。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.utils.text import normalize_whitespace, similarity
from app.utils.tracing import utc_now

KIND_RULE = "rule"
KIND_IGNORE = "ignore"

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

KIND_LABELS = {KIND_RULE: "要求", KIND_IGNORE: "不改"}


def next_rule_id(rules: Iterable[dict[str, Any]]) -> str:
    """下一个编号：R1、R2……（撤销过的不复用）。"""

    highest = 0
    for rule in rules or []:
        match = re.match(r"^R(\d+)$", str(rule.get("id") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"R{highest + 1}"


def active_rules(rules: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        rule
        for rule in (rules or [])
        if rule.get("status", STATUS_ACTIVE) == STATUS_ACTIVE
    ]


def rule_texts(rules: Iterable[dict[str, Any]] | None) -> list[str]:
    return [str(rule.get("text") or "").strip() for rule in active_rules(rules) if rule.get("text")]


def add_rule(
    rules: list[dict[str, Any]],
    text: str,
    *,
    kind: str = KIND_RULE,
    turn: int = 1,
    source: str = "user",
    issue_ids: list[int] | None = None,
    signature: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """新增一条要求或“不改”的决定（文本为空则跳过）。"""

    text = str(text or "").strip()
    if not text:
        return rules, None

    now = utc_now()
    rule = {
        "id": next_rule_id(rules),
        "kind": kind,
        "text": text,
        "issue_ids": list(issue_ids or []),
        "signature": signature or {},
        "created_turn": turn,
        "updated_turn": turn,
        "status": STATUS_ACTIVE,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    return [*rules, rule], rule


def ignore_issues(
    rules: list[dict[str, Any]],
    issues: Iterable[dict[str, Any]],
    *,
    turn: int = 1,
    source: str = "issue_review",
    note: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    把“这些问题不改”记成持久决定。

    存的是**指纹**（位置 + 分类 + 问题摘要），因此下一轮重新审查时
    即使问题编号变了，也能认出是同一个问题、不再问一次。
    """

    created: list[dict[str, Any]] = []
    for issue in issues:
        # 同一个问题重复忽略时不再新增一条（避免清单膨胀）
        if is_ignored(issue, rules):
            continue
        text = _ignore_text(issue, note)
        rules, rule = add_rule(
            rules,
            text,
            kind=KIND_IGNORE,
            turn=turn,
            source=source,
            issue_ids=[issue.get("id")] if issue.get("id") is not None else [],
            signature=issue_signature(issue),
        )
        if rule is not None:
            created.append(rule)
    return rules, created


def _ignore_text(issue: dict[str, Any], note: str) -> str:
    label = issue.get("span_label") or issue.get("location") or "全文"
    problem = normalize_whitespace(str(issue.get("problem") or ""))[:60]
    text = f"不改 {label}：{problem}"
    if note:
        text += f"（{note}）"
    return text


def issue_signature(issue: dict[str, Any]) -> dict[str, Any]:
    """问题的“指纹”，用于跨轮次识别同一问题。"""

    return {
        "span_label": str(issue.get("span_label") or ""),
        "spans": [
            str(label)
            for label in (
                [issue.get("span_label")] + list(issue.get("affected_spans") or [])
            )
            if label
        ],
        "category": str(issue.get("category") or ""),
        "problem": normalize_whitespace(str(issue.get("problem") or ""))[:80],
        "evidence": normalize_whitespace(str(issue.get("evidence") or ""))[:40],
    }


def revoke_rule(rules: list[dict[str, Any]], rule_id: str, *, turn: int = 1) -> list[dict[str, Any]]:
    """撤销一条（用户改主意了）。"""

    target = str(rule_id or "").strip().upper()
    updated: list[dict[str, Any]] = []
    for rule in rules:
        item = dict(rule)
        if str(item.get("id", "")).upper() == target:
            item["status"] = STATUS_REVOKED
            item["updated_turn"] = turn
            item["updated_at"] = utc_now()
        updated.append(item)
    return updated


def update_rule(
    rules: list[dict[str, Any]],
    rule_id: str,
    text: str,
    *,
    turn: int = 1,
) -> list[dict[str, Any]]:
    """修改一条要求的措辞（语义以用户最新说法为准）。"""

    target = str(rule_id or "").strip().upper()
    text = str(text or "").strip()
    updated: list[dict[str, Any]] = []
    for rule in rules:
        item = dict(rule)
        if str(item.get("id", "")).upper() == target and text:
            item["text"] = text
            item["updated_turn"] = turn
            item["updated_at"] = utc_now()
            item["status"] = STATUS_ACTIVE
        updated.append(item)
    return updated


def ignored_signatures(rules: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        dict(rule.get("signature") or {})
        for rule in active_rules(rules)
        if rule.get("kind") == KIND_IGNORE
    ]


def is_ignored(issue: dict[str, Any], rules: Iterable[dict[str, Any]] | None) -> bool:
    """
    这条问题是不是用户已经明确说过“不用改”。

    判定顺序（从严到宽）：同一段落 + 同一分类 → 同一问题；
    同一段落 + 引文高度重合 → 同一问题；
    问题描述足够长且高度相似 → 同一问题。

    特意不做“短句模糊匹配”：像“问题一 / 问题二”这种短描述
    相似度会虚高，容易把不同问题误判成同一个。
    """

    signature = issue_signature(issue)
    for stored in ignored_signatures(rules):
        if not stored:
            continue

        same_label = bool(
            stored.get("span_label")
            and signature.get("span_label")
            and stored["span_label"] == signature["span_label"]
        )
        # 合并过的系统性问题会带一串受影响段落，任一命中即算同一个问题
        stored_spans = set(stored.get("spans") or [])
        if stored_spans and stored_spans & set(signature.get("spans") or []):
            return True
        same_category = bool(
            stored.get("category")
            and signature.get("category")
            and stored["category"] == signature["category"]
        )

        if same_label and (same_category or not stored.get("category") or not signature.get("category")):
            return True

        if same_label and stored.get("evidence") and signature.get("evidence"):
            if similarity(stored["evidence"], signature["evidence"]) >= 0.8:
                return True

        left = str(stored.get("problem") or "")
        right = str(signature.get("problem") or "")
        if left and right and left == right:
            return True  # 描述完全一致 → 同一个问题
        if len(left) >= 12 and len(right) >= 12 and similarity(left, right) >= 0.7:
            return True

    return False


def describe(rules: Iterable[dict[str, Any]] | None, *, kinds: tuple[str, ...] | None = None) -> str:
    """给 Prompt 用的文本（带编号，方便用户按编号改）。"""

    items = [
        rule
        for rule in active_rules(rules)
        if kinds is None or rule.get("kind") in kinds
    ]
    if not items:
        return ""
    lines = []
    for rule in items:
        label = KIND_LABELS.get(str(rule.get("kind")), "要求")
        lines.append(
            f"- [{rule.get('id')}][{label}] {rule.get('text')}"
            f"（第 {rule.get('created_turn', 1)} 轮提出）"
        )
    return "\n".join(lines)


def apply_payload(
    rules: list[dict[str, Any]],
    payload: Any,
    *,
    turn: int = 1,
    source: str = "user",
    issues: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    解析用户在任意环节输入的要求/决定，返回 (新规则列表, 本次新增的记录)。

    支持这些写法：

        {"rules": ["土豆统一写成马铃薯"]}                 # 新增要求
        {"revoke": ["R1"]}                                 # 撤销要求
        {"update": {"R1": "改成……"}}                       # 修改要求
        {"ignore_ids": [1, 3]}                             # 这几个问题不改
        {"ignore_all": true}                               # 全部不改
    """

    payload = payload if isinstance(payload, dict) else {}
    created: list[dict[str, Any]] = []

    # 1) 新增要求
    raw_rules = payload.get("rules") or payload.get("constraints")
    for text in _split_rules(raw_rules):
        rules, rule = add_rule(rules, str(text), kind=KIND_RULE, turn=turn, source=source)
        if rule is not None:
            created.append(rule)

    # 2) 撤销
    raw_revoke = payload.get("revoke") or payload.get("revoke_ids")
    if isinstance(raw_revoke, str):
        raw_revoke = [piece.strip() for piece in raw_revoke.replace("，", ",").split(",") if piece.strip()]
    for rule_id in raw_revoke or []:
        rules = revoke_rule(rules, str(rule_id), turn=turn)

    # 3) 修改
    raw_update = payload.get("update") or {}
    if isinstance(raw_update, dict):
        for rule_id, text in raw_update.items():
            rules = update_rule(rules, str(rule_id), str(text), turn=turn)

    # 4) 忽略问题
    issue_list = list(issues or [])
    by_id = {issue.get("id"): issue for issue in issue_list}
    ignore_ids = payload.get("ignore_ids")
    selected: list[dict[str, Any]] = []
    if payload.get("ignore_all"):
        selected = issue_list
    elif isinstance(ignore_ids, str):
        wanted = {
            int(piece) for piece in ignore_ids.replace("，", ",").split(",") if piece.strip().isdigit()
        }
        selected = [issue for issue in issue_list if issue.get("id") in wanted]
    elif isinstance(ignore_ids, (list, tuple, set)):
        wanted = {int(item) for item in ignore_ids if str(item).strip().isdigit()}
        selected = [issue for issue in issue_list if issue.get("id") in wanted]

    note = str(payload.get("note") or "").strip()
    if selected:
        rules, ignored = ignore_issues(
            rules, selected, turn=turn, source=source, note=note
        )
        created.extend(ignored)
        # 顺便把问题本身标记出来，报告里能看到“按用户要求忽略”
        for issue in selected:
            if issue.get("id") in by_id:
                by_id[issue.get("id")]["ignored"] = True

    return rules, created


def _split_rules(raw: Any) -> list[str]:
    """把用户/界面传来的要求拆成一条条（支持列表、分号、换行混写）。"""

    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    texts: list[str] = []
    for item in items:
        for piece in str(item).replace("；", "\n").replace(";", "\n").split("\n"):
            cleaned = piece.strip(" -•\t")
            if cleaned:
                texts.append(cleaned)
    return texts


_REVOKE_RE = re.compile(r"^\s*(?:取消|撤销|不用|删掉)\s*(R\d+)\s*$", re.IGNORECASE)
_UPDATE_RE = re.compile(r"^\s*(R\d+)\s*(?:改成|改为|调整为|更新为)\s*(.+)$", re.IGNORECASE)
_ADD_RE = re.compile(r"^\s*(?:记住|要求|新要求)\s*[:：]\s*(.+)$")


def parse_instruction(instruction: str) -> tuple[str, dict[str, Any]]:
    """
    从用户这一轮的话里拆出“改要求”的指令。

    支持这些写法（其余内容仍作为本轮的修改要求）：

        记住：全文统一把「土豆」写成「马铃薯」     -> 新增要求
        R1 改成 全文统一用「马铃薯」              -> 修改要求
        取消 R1                                   -> 撤销要求
    """

    text = str(instruction or "")
    payload: dict[str, Any] = {}
    remaining: list[str] = []

    # 一行里可以用「；」分隔多条指令（命令行里输入换行不方便）
    segments: list[str] = []
    for line in text.splitlines():
        segments.extend(piece.strip() for piece in line.split("；") if piece.strip())

    for stripped in segments:
        match = _REVOKE_RE.match(stripped)
        if match:
            payload.setdefault("revoke", []).append(match.group(1).upper())
            continue

        match = _UPDATE_RE.match(stripped)
        if match:
            payload.setdefault("update", {})[match.group(1).upper()] = match.group(2).strip()
            continue

        match = _ADD_RE.match(stripped)
        if match:
            payload.setdefault("rules", []).append(match.group(1).strip())
            continue

        remaining.append(stripped)

    return "\n".join(remaining).strip(), payload
