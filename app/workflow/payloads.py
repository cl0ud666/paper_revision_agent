"""Pure normalization and public views for human decisions.

This module does not invoke LangGraph or agents. Nodes own interruption and state
transitions; CLI, Streamlit and web adapters share these payload semantics.
"""

from __future__ import annotations

from typing import Any

from app.document.patches import STATUS_ACCEPTED, STATUS_CONFLICT, STATUS_PROPOSED, STATUS_REJECTED
from app.utils.rules import apply_payload as apply_rules_payload, rule_texts
from app.utils.tracing import utc_now


def normalize_issue_payload(
    payload: Any,
    findings: list[dict[str, Any]],
    *,
    rules: list[dict[str, Any]] | None = None,
    turn: int = 1,
) -> dict[str, Any]:
    """规范化问题闸门的输入，并把决定写进user_rules。"""

    payload = payload if isinstance(payload, dict) else {}
    updated_findings = [dict(issue) for issue in findings]
    from app.document.review_items import manual_item, manual_reason

    selected = list(payload.get("selected_decisions") or [])
    for issue in updated_findings:
        choice = next((d for d in selected if d["issue_id"] == issue.get("id")), None)
        if choice:
            choice["manual_reason"] = manual_reason(choice["selected_option"])
            issue["selected_option"] = choice["selected_option"]
            issue["suggestion"] = choice["selected_option"]
            if choice["manual_reason"]:
                issue.update(
                    kind="advice", manual_required=True, blocked_reason=choice["manual_reason"]
                )

    # 兼容旧写法：{"approved_issue_ids": [1, 3]} 表示“只改这些”
    if "approved_issue_ids" in payload:
        wanted = _as_id_set(payload.get("approved_issue_ids"))
        payload = {
            **payload,
            "ignore_ids": [
                issue.get("id")
                for issue in updated_findings
                if not issue.get("ignored") and str(issue.get("id")) not in wanted
            ],
        }

    new_rules, created = apply_rules_payload(
        list(rules or []),
        payload,
        turn=turn,
        source="issue_review",
        issues=updated_findings,
    )
    ignored_ids = [issue.get("id") for issue in updated_findings if issue.get("ignored")]

    return {
        "selected_decisions": selected,
        "manual_items": [
            manual_item(i, i["blocked_reason"])
            for i in updated_findings
            if i.get("manual_required") and not i.get("ignored")
        ],
        "user_rules": new_rules,
        "human_constraints": rule_texts(new_rules),
        "audit_findings": updated_findings,
        "rules_added": created,
        "approved_issue_ids": [
            issue.get("id") for issue in updated_findings if not issue.get("ignored")
        ],
        "human_decision": "approve",
        "human_rounds": [
            {
                "type": "issue_review",
                "turn": turn,
                "ignored": ignored_ids,
                "approved": [
                    issue.get("id") for issue in updated_findings if not issue.get("ignored")
                ],
                "added_rules": [rule.get("id") for rule in created],
                "timestamp": utc_now(),
            }
        ],
    }


ISSUE_FIELDS = (
    "id",
    "kind",
    "options",
    "span_labels",
    "span_label",
    "category",
    "severity",
    "location",
    "problem",
    "evidence",
    "suggestion",
    "ignored",
    "warning",
)


def _public_issue(issue: dict[str, Any]) -> dict[str, Any]:
    return {key: issue.get(key) for key in ISSUE_FIELDS if issue.get(key) is not None}


def _excluded_ids(payload: Any, patches: list[dict[str, Any]]) -> list[str]:
    """解析"剔除哪几条"：支持excluded_ids / 编号 / none。"""

    data = payload if isinstance(payload, dict) else {}
    if data.get("exclude_all"):
        return [str(patch.get("patch_id") or "") for patch in patches]

    raw = data.get("excluded_ids") or data.get("numbers") or []
    if isinstance(raw, str):
        raw = [piece.strip() for piece in raw.replace("，", ",").split(",") if piece.strip()]

    known = {str(patch.get("patch_id") or "") for patch in patches}
    by_number = {
        str(position): str(patch.get("patch_id") or "")
        for position, patch in enumerate(patches, start=1)
    }

    wanted: list[str] = []
    for item in raw:
        key = str(item).strip()
        if key in by_number:
            wanted.append(by_number[key])
        elif key in known:
            wanted.append(key)
    return [patch_id for patch_id in dict.fromkeys(wanted) if patch_id]


# ---------------------------------------------------------------- 工具函数


def normalize_human_payload(payload: Any, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """
    规范化人工确认的输入，容忍多种写法。

    支持：

    - approved_issue_ids: [1, 2] / ["all"] / ["none"] / 1 / "1,2"
    - constraints: ["..."] 或 "用分号/换行分隔的字符串"
    - decision: approve / reject（默认approve）
    """

    all_ids = [issue.get("id") for issue in findings if issue.get("id") is not None]
    payload = payload if isinstance(payload, dict) else {}

    decision = str(payload.get("decision") or "approve").strip().lower()
    if decision not in {"approve", "reject"}:
        decision = "approve"

    approved_ids = _parse_ids(payload.get("approved_issue_ids"), all_ids)

    raw_constraints = payload.get("constraints")
    if raw_constraints is None:
        constraints: list[str] = []
    elif isinstance(raw_constraints, str):
        pieces = raw_constraints.replace("；", "\n").replace(";", "\n").split("\n")
        constraints = [piece.strip(" -•\t") for piece in pieces]
        constraints = [piece for piece in constraints if piece]
    else:
        constraints = [str(item).strip() for item in raw_constraints if str(item).strip()]

    feedback = str(payload.get("feedback") or "").strip()

    approved_set = set(approved_ids)
    updated_findings: list[dict[str, Any]] = []
    for issue in findings:
        item = dict(issue)
        item["approved"] = item.get("id") in approved_set
        updated_findings.append(item)

    return {
        "approved_issue_ids": approved_ids,
        "human_constraints": constraints,
        "human_feedback": feedback,
        "human_decision": decision,
        "audit_findings": updated_findings,
        "human_rounds": [
            {
                "iteration": payload.get("iteration"),
                "decision": decision,
                "approved": approved_ids,
                "constraints": constraints,
                "feedback": feedback,
                "timestamp": utc_now(),
            }
        ],
    }


def _parse_ids(raw_ids: Any, all_ids: list[Any]) -> list[int]:
    """把各种写法的问题编号统一成int列表。"""

    if raw_ids is None:
        return [int(issue_id) for issue_id in all_ids]

    if isinstance(raw_ids, str):
        text = raw_ids.strip().lower()
        if text in {"all", "全部", "*", ""}:
            return [int(issue_id) for issue_id in all_ids]
        if text in {"none", "无", "不修改"}:
            return []
        candidates: list[Any] = text.replace("，", ",").split(",")
    elif isinstance(raw_ids, (int, float)):
        candidates = [raw_ids]
    else:
        candidates = list(raw_ids or [])

    parsed: list[int] = []
    for item in candidates:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            parsed.append(int(item))
        elif isinstance(item, str) and item.strip().isdigit():
            parsed.append(int(item.strip()))

    allowed = {int(issue_id) for issue_id in all_ids}
    return [issue_id for issue_id in parsed if issue_id in allowed]


PATCH_FIELDS = (
    "patch_id",
    "issue_id",
    "span_id",
    "span_label",
    "sentence_ref",
    "position",
    "strategy",
    "status",
    "status_label",
    "conflict_code",
    "before",
    "replace",
    "rationale",
    "category",
    "severity",
    "conflict_reason",
    "warnings",
    "round",
    "decided_by",
    "decision_note",
)


def _patch_line(patch: dict[str, Any]) -> str:
    """
    Revision报给用户看的格式（批注闸门关闭时由它输出）。

    固定三行——原文 / 准备修改为 / 原因，和Word批注保持一致，
    用户看一遍就知道要改什么、为什么改，不需要再逐条确认。
    """

    where = patch.get("position") or patch.get("span_label") or patch.get("span_id") or ""
    lines = [f"[{patch.get('patch_id')}] {where}"]
    before = str(patch.get("before") or patch.get("quote") or "").strip()
    after = "（删除）" if patch.get("delete") else str(patch.get("replace") or "").strip()
    reason = str(patch.get("rationale") or "").strip()
    if before:
        lines.append(f"    原文：{before}")
    if after:
        lines.append(f"    准备修改为：{after}")
    if reason:
        lines.append(f"    原因：{reason}")
    return "\n".join(lines)


def _public_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """给前端 / 命令行看的补丁视图（不含内部定位偏移）。"""

    return {key: patch.get(key) for key in PATCH_FIELDS if patch.get(key) is not None}


def normalize_patch_payload(payload: Any, patches: list[dict[str, Any]]) -> dict[str, Any]:
    """
    把用户的补丁决定规范化。

    默认是**全部接受**（界面上复选框默认勾选），
    用户只需要取消掉不想要的几条，不用写理由。
    """

    payload = payload if isinstance(payload, dict) else {}
    decidable = [patch for patch in patches if patch.get("status") == STATUS_PROPOSED]
    decidable_ids = [str(patch.get("patch_id")) for patch in decidable]

    # 1) 逐条decisions（最灵活）
    decision_map: dict[str, str] = {}
    raw_decisions = payload.get("decisions")
    if isinstance(raw_decisions, dict):
        for patch_id, value in raw_decisions.items():
            decision = value.get("decision") if isinstance(value, dict) else value
            decision_map[str(patch_id)] = str(decision or "").strip().lower()
    elif isinstance(raw_decisions, list):
        for item in raw_decisions:
            if isinstance(item, dict) and item.get("patch_id"):
                decision_map[str(item["patch_id"])] = (
                    str(item.get("decision") or "accept").strip().lower()
                )
            elif isinstance(item, str):
                decision_map[item] = "accept"

    # 2) 白名单 / 黑名单
    accepted_ids = payload.get("accepted_ids")
    rejected_ids = payload.get("rejected_ids")
    if not decision_map and isinstance(accepted_ids, (list, tuple, set, str)):
        wanted = _as_id_set(accepted_ids)
        decision_map = {
            patch_id: ("accept" if patch_id in wanted else "reject") for patch_id in decidable_ids
        }
    elif not decision_map and isinstance(rejected_ids, (list, tuple, set, str)):
        unwanted = _as_id_set(rejected_ids)
        decision_map = {
            patch_id: ("reject" if patch_id in unwanted else "accept") for patch_id in decidable_ids
        }

    # 3) 全接受 / 全拒绝；都没给就默认全接受
    if not decision_map:
        default = "reject" if payload.get("reject_all") else "accept"
        decision_map = {patch_id: default for patch_id in decidable_ids}

    note = str(payload.get("note") or payload.get("feedback") or "").strip()
    now = utc_now()
    updated: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for patch in patches:
        item = dict(patch)
        patch_id = str(item.get("patch_id"))
        if item.get("status") == STATUS_PROPOSED:
            decision = decision_map.get(patch_id, "accept")
            if decision not in {"accept", "reject"}:
                decision = "accept"
            if decision == "accept":
                item["status"] = STATUS_ACCEPTED
                accepted.append(item)
            else:
                item["status"] = STATUS_REJECTED
                rejected.append(item)
            item["decided_by"] = "user"
            item["decided_at"] = now
            if note:
                item["decision_note"] = note
        elif item.get("status") == STATUS_REJECTED:
            rejected.append(item)
        elif item.get("status") == STATUS_ACCEPTED:
            accepted.append(item)
        updated.append(item)

    rejected_memory = [
        {
            "patch_id": patch.get("patch_id"),
            "span_id": patch.get("span_id"),
            "span_label": patch.get("span_label"),
            "before": patch.get("before"),
            "replace": patch.get("replace"),
            "rationale": patch.get("rationale"),
            "note": patch.get("decision_note", ""),
            "turn": payload.get("turn"),
            "round": patch.get("round"),
            "at": now,
        }
        for patch in rejected
        if patch.get("decided_by") == "user"
    ]

    stats = {
        "proposed": sum(1 for patch in updated if patch.get("status") == STATUS_PROPOSED),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "applied": 0,
        "conflict": sum(1 for patch in updated if patch.get("status") == STATUS_CONFLICT),
    }

    return {
        "proposed_patches": updated,
        "patch_decisions": {
            patch.get("patch_id"): {
                "decision": "accept" if patch.get("status") == STATUS_ACCEPTED else "reject",
                "note": patch.get("decision_note", ""),
                "decided_by": patch.get("decided_by", "user"),
                "at": patch.get("decided_at", now),
            }
            for patch in updated
            if patch.get("status") in {STATUS_ACCEPTED, STATUS_REJECTED}
        },
        "patch_stats": stats,
        "rejected_patches": [patch for patch in rejected if patch.get("decided_by") == "user"],
        "rejected_memory": rejected_memory,
        "human_rounds": [
            {
                "type": "patch_review",
                "turn": payload.get("turn"),
                "accepted": [patch.get("patch_id") for patch in accepted],
                "rejected": [patch.get("patch_id") for patch in rejected],
                "note": note,
                "timestamp": now,
            }
        ],
    }


def _as_id_set(value: Any) -> set[str]:
    if isinstance(value, str):
        parts = value.replace("，", ",").split(",")
        return {part.strip() for part in parts if part.strip()}
    return {str(item).strip() for item in value if str(item).strip()}
