"""CLI、Streamlit 和服务/API 共用的人工交互协议。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

REQUEST_TYPES = ("issue_review", "patch_review", "final_confirm", "human_approval")


def request_type(request: Mapping[str, Any]) -> str:
    kind = request.get("type")
    if kind not in REQUEST_TYPES:
        raise ValueError(f"不支持的人工请求类型：{kind!r}")
    return str(kind)


def dispatch_request(request: dict[str, Any], handlers: Mapping[str, Callable]) -> Any:
    """按明确的请求类型选择界面，未知类型直接报错。"""
    return handlers[request_type(request)](request)


def prepare_response(request: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """统一上下文校验、结构化选项和最终确认分组的展开。"""
    kind = request_type(request)
    if not isinstance(payload, dict):
        raise ValueError("人工回答必须是 JSON 对象")
    data = deepcopy(payload)
    for field in ("turn", "iteration"):
        if field in request:
            if data.get(field) is not None and data[field] != request[field]:
                raise ValueError(f"回答的 {field} 已过期，请刷新当前请求")
            data[field] = request[field]
    if kind == "issue_review" and "choices" in data:
        issues = {str(issue["id"]): issue for issue in request.get("issues", [])}
        choices = data.pop("choices")
        if not isinstance(choices, dict):
            raise ValueError("choices 必须为 {issue_id: option_index} 对象")
        rules = list(data.get("rules") or [])
        for issue_id, index in choices.items():
            issue = issues.get(str(issue_id))
            if issue is None or not isinstance(index, int) or isinstance(index, bool):
                raise ValueError("无效的问题编号或选项索引")
            options = issue.get("options") or []
            if not 0 <= index < len(options):
                raise ValueError("选项索引超出范围")
            where = issue.get("span_label") or issue.get("location") or f"问题 {issue_id}"
            rules.append(f"{where}：{options[index]}")
        data["rules"] = rules
        ignored = list(data.get("ignore_ids") or [])
        # 明确未作选择的条目可以保留；有自由规则时由规则决定处理范围。
        if not data.get("rules"):
            ignored.extend(issue["id"] for issue in issues.values())
        elif not payload.get("rules"):
            ignored.extend(issue["id"] for key, issue in issues.items() if key not in {str(k) for k in choices})
        data["ignore_ids"] = list(dict.fromkeys(ignored))
    if kind == "final_confirm" and not data.get("exclude_all"):
        patches = request.get("patches") or []
        members = {
            str(patch.get("patch_id")): [str(i) for i in patch.get("patch_ids") or [patch.get("patch_id")]]
            for patch in patches
        }
        allowed = {member for values in members.values() for member in values}
        by_number = {str(i): str(patch.get("patch_id")) for i, patch in enumerate(patches, 1)}
        raw = data.get("excluded_ids", data.get("numbers", request.get("defaults", {}).get("excluded_ids", [])))
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
        if not isinstance(raw, list):
            raise ValueError("excluded_ids 必须为编号列表")
        excluded = []
        for value in raw:
            key = by_number.get(str(value), str(value))
            if key in members:
                excluded.extend(members[key])
            elif key in allowed:
                excluded.append(key)
            else:
                raise ValueError(f"未知的最终确认补丁：{value}")
        data.pop("numbers", None)
        data["excluded_ids"] = list(dict.fromkeys(excluded))
    return data


def auto_response(request: dict[str, Any], constraints: list[str] | None = None) -> dict[str, Any]:
    kind = request_type(request)
    payloads = {
        "patch_review": {"accept_all": True},
        "issue_review": {"ignore_ids": [], "rules": list(constraints or [])},
        "final_confirm": {"excluded_ids": request.get("defaults", {}).get("excluded_ids", [])},
        "human_approval": {
            "decision": "approve",
            "approved_issue_ids": request.get("defaults", {}).get("approved_issue_ids") or "all",
            "constraints": list(constraints or []),
        },
    }
    return prepare_response(request, payloads[kind])
