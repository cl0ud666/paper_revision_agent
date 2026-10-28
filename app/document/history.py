"""Read accumulated edit history, including persisted pre-patch snapshots."""

from typing import Any


def all_applied_edits(state: Any) -> list[dict[str, Any]]:
    """汇总所有轮次真正应用的补丁（报告与界面用）。"""

    collected: list[dict[str, Any]] = []
    for entry in state.get("applied_patches") or []:
        collected.append(dict(entry))
    if collected:
        return collected
    # 兼容旧数据（没有补丁模型时用的revision_edits）
    return list(state.get("revision_edits") or [])


def all_failed_edits(state: Any) -> list[dict[str, Any]]:
    """汇总所有无法定位的补丁。"""

    collected = [dict(entry) for entry in (state.get("conflict_patches") or [])]
    if collected:
        return collected
    return list(state.get("revision_failed_edits") or [])
