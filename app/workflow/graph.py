"""
构建论文修改 Workflow（LangGraph StateGraph）。

正常路径（稳定、可预测）：

    START → Orchestrator → Audit → Revision（提出补丁）
          → Patch Review（用户逐条接受/拒绝，唯一的人工闸门）
          → Apply（把接受的补丁一次性应用） → Review → Report → END

异常路径（只有出现问题时才重新唤起 Planner）：

    Review FAIL → Orchestrator（Adaptive Re-planning）→ Audit / Revision / Report

Human Approval 通过 ``interrupt`` 暂停，因此 Graph 必须带 Checkpointer 编译。
（``PRA_ISSUE_GATE=1`` 时会在 Audit 之后额外加一道“先确认要改哪些问题”的闸门，
默认关闭，避免让用户来回确认两次。）
"""

from __future__ import annotations

import threading
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.config import Settings, get_settings
from app.state.schema import PaperState
from app.workflow.nodes import (
    apply_node,
    audit_node,
    issue_review_node,
    orchestrator_node,
    patch_review_node,
    report_node,
    review_node,
    revision_node,
)
from app.workflow.router import (
    route_after_audit,
    route_after_issue_review,
    route_after_patch_review,
    route_after_review,
    route_from_orchestrator,
)

# 同一个 SQLite 文件只开一条连接、共用一个 Saver。
# 为什么必须这样：并发跑多个任务时，若每个任务各自建连接，
# 建表 / 写入会互相抢锁（database is locked），Checkpoint 会失效。
_CHECKPOINTERS: dict[str, Any] = {}
_CHECKPOINTERS_LOCK = threading.Lock()


def build_checkpointer(settings: Settings | None = None) -> Any:
    """
    创建 Checkpointer。

    默认使用内存 Checkpointer（同一进程内可暂停 / 恢复）。
    如果安装了 ``langgraph-checkpoint-sqlite`` 并配置了 ``PRA_CHECKPOINT_DB``，
    则使用 SQLite，实现跨进程的持久化恢复（例如“用户第二天回来继续”）。
    """

    settings = settings or get_settings()
    db_path = settings.workflow.checkpoint_db
    if db_path:
        try:
            import sqlite3
            from pathlib import Path

            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore

            path = Path(db_path)
            if not path.is_absolute():
                path = settings.paths.project_root / path

            key = str(path.resolve())
            with _CHECKPOINTERS_LOCK:
                cached = _CHECKPOINTERS.get(key)
                if cached is not None:
                    return cached

                path.parent.mkdir(parents=True, exist_ok=True)
                # check_same_thread=False：Web 服务里同一个 Graph 会被不同线程调用
                conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA synchronous=NORMAL")
                saver = SqliteSaver(conn)
                saver.setup()
                _CHECKPOINTERS[key] = saver
                return saver
        except Exception as exc:  # noqa: BLE001 - 未安装时退回内存 Checkpointer
            import warnings

            warnings.warn(
                f"SQLite Checkpoint 不可用（{exc}），退回内存 Checkpointer："
                "服务重启后无法恢复暂停中的任务。",
                RuntimeWarning,
            )
    return InMemorySaver()


def reset_checkpointers() -> None:
    """关闭并清空 Checkpointer 缓存（测试用）。"""

    with _CHECKPOINTERS_LOCK:
        for key, saver in list(_CHECKPOINTERS.items()):
            try:
                saver.conn.close()
            except Exception:  # noqa: BLE001
                pass
            _CHECKPOINTERS.pop(key, None)


def build_graph(
    *,
    checkpointer: Any | None = None,
    settings: Settings | None = None,
) -> Any:
    """创建并编译 Workflow。"""

    settings = settings or get_settings()
    builder = StateGraph(PaperState)

    # ---------------- 1. 注册 Nodes ----------------
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("audit", audit_node)
    builder.add_node("issue_review", issue_review_node)
    builder.add_node("revision", revision_node)
    builder.add_node("patch_review", patch_review_node)
    builder.add_node("apply", apply_node)
    builder.add_node("review", review_node)
    builder.add_node("report", report_node)

    # ---------------- 2. 入口 ----------------
    builder.add_edge(START, "orchestrator")

    # ---------------- 3. Orchestrator 的条件路由 ----------------
    builder.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "audit": "audit",
            "revision": "revision",
            # Orchestrator 说“需要人拍板”时，统一走问题闸门（列出问题问用户）
            "human_approval": "issue_review",
            "report": "report",
        },
    )

    # ---------------- 4. Audit 之后 ----------------
    builder.add_conditional_edges(
        "audit",
        route_after_audit,
        {
            "issue_review": "issue_review",
            "revision": "revision",
            "report": "report",
        },
    )

    # ---------------- 5. 问题闸门之后 ----------------
    builder.add_conditional_edges(
        "issue_review",
        route_after_issue_review,
        {
            "revision": "revision",
            "report": "report",
        },
    )

    # ---------------- 6. 提补丁 → 补丁审阅 → 应用 ----------------
    builder.add_edge("revision", "patch_review")

    builder.add_conditional_edges(
        "patch_review",
        route_after_patch_review,
        {
            "apply": "apply",
            "report": "report",
        },
    )

    builder.add_edge("apply", "review")

    # ---------------- 7. 复核之后的闭环 ----------------
    builder.add_conditional_edges(
        "review",
        route_after_review,
        {
            "report": "report",
            "orchestrator": "orchestrator",
        },
    )

    # ---------------- 8. 报告结束 ----------------
    builder.add_edge("report", END)

    return builder.compile(checkpointer=checkpointer or build_checkpointer(settings))


def mermaid(graph: Any) -> str:
    """导出 Graph 的 Mermaid 结构（用于文档与调试）。"""

    return graph.get_graph().draw_mermaid()
