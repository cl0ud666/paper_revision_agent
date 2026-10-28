"""
构建论文修改Workflow（LangGraph StateGraph）。

正常路径（稳定、可预测）：

    START → Orchestrator → Audit → Revision（提出补丁）
          → Patch Review（用户逐条接受/拒绝，唯一的人工闸门）
          → Apply（把接受的补丁一次性应用） → Review → Report → END

异常路径（只有出现问题时才重新唤起Planner）：

    Review FAIL → Orchestrator（Adaptive Re-planning）→ Audit / Revision / Report

Human Approval通过``interrupt``暂停，因此Graph必须带Checkpointer编译。
问题闸门只对Audit标记为``decision``的前后矛盾生效；普通fix/advice不额外打断用户。
``PRA_ISSUE_GATE``作为兼容配置保留，但不改变这一决策类型边界。
"""

from __future__ import annotations

import os
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
    prepare_report_node,
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

# 同一个SQLite文件只开一条连接、共用一个Saver。
# 为什么必须这样：并发跑多个任务时，若每个任务各自建连接，
# 建表 / 写入会互相抢锁（database is locked），Checkpoint会失效。
_CHECKPOINTERS: dict[str, Any] = {}
_CHECKPOINTERS_LOCK = threading.Lock()


def build_checkpointer(settings: Settings | None = None) -> Any:
    """
    创建Checkpointer。

    auto模式配置PRA_CHECKPOINT_URL后使用PostgreSQL，否则本地使用SQLite。
    PostgreSQL不可用时明确失败，避免生产任务静默退回内存而无法恢复。
    显式memory仅用于开发和测试。
    """

    settings = settings or get_settings()
    backend = settings.workflow.checkpoint_backend
    url = settings.workflow.checkpoint_url
    if backend == "auto":
        backend = "postgres" if url else "sqlite"
    if backend not in {"postgres", "sqlite", "memory"}:
        raise ValueError("PRA_CHECKPOINT_BACKEND必须为auto/postgres/sqlite/memory")
    if backend == "memory":
        return InMemorySaver()
    if backend == "postgres":
        if not url:
            raise ValueError("PostgreSQL Checkpointer需要配置PRA_CHECKPOINT_URL")
        # 连接池按进程创建；Celery prefork不能复用父进程的数据库连接。
        key = f"postgres:{os.getpid()}:{url}"
        with _CHECKPOINTERS_LOCK:
            cached = _CHECKPOINTERS.get(key)
            if cached is not None:
                return cached
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
                from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError(
                    "请安装requirements-web.txt中的PostgreSQL Checkpoint依赖"
                ) from exc
            pool = None
            try:
                pool = ConnectionPool(
                    conninfo=url,
                    min_size=1,
                    max_size=settings.workflow.checkpoint_pool_size,
                    kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
                    open=True,
                )
                pool.wait(timeout=10)
                saver = PostgresSaver(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
            except Exception:
                if pool is not None:
                    pool.close()
                # 不在错误中泄露包含密码的数据库URL。
                raise RuntimeError(
                    "PostgreSQL Checkpointer连接失败；请检查数据库配置和可用性"
                ) from None
            _CHECKPOINTERS[key] = saver
            return saver
    db_path = settings.workflow.checkpoint_db
    if db_path:
        try:
            import sqlite3
            from pathlib import Path

            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore

            path = Path(db_path)
            if not path.is_absolute():
                path = settings.paths.project_root / path

            key = f"sqlite:{os.getpid()}:{path.resolve()}"
            with _CHECKPOINTERS_LOCK:
                cached = _CHECKPOINTERS.get(key)
                if cached is not None:
                    return cached

                path.parent.mkdir(parents=True, exist_ok=True)
                # check_same_thread=False：Web服务里同一个Graph会被不同线程调用
                conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA synchronous=NORMAL")
                saver = SqliteSaver(conn)
                saver.setup()
                _CHECKPOINTERS[key] = saver
                return saver
        except Exception as exc:  # noqa: BLE001 - 未安装时退回内存Checkpointer
            import warnings

            warnings.warn(
                f"SQLite Checkpoint不可用（{exc}），退回内存Checkpointer："
                "服务重启后无法恢复暂停中的任务。",
                RuntimeWarning,
            )
    return InMemorySaver()


def reset_checkpointers() -> None:
    """关闭并清空Checkpointer缓存（测试用）。"""

    with _CHECKPOINTERS_LOCK:
        for key, saver in list(_CHECKPOINTERS.items()):
            try:
                saver.conn.close()
            except Exception:  # noqa: BLE001
                pass
            _CHECKPOINTERS.pop(key, None)


def setup_checkpointer() -> None:
    """部署时执行一次；PostgresSaver的建表迁移不放在每次任务里。"""
    saver = build_checkpointer()
    if isinstance(saver, InMemorySaver):
        raise RuntimeError("内存Checkpointer无需初始化，请配置持久化后端")
    saver.setup()


def build_graph(
    *,
    checkpointer: Any | None = None,
    settings: Settings | None = None,
) -> Any:
    """创建并编译Workflow。"""

    settings = settings or get_settings()
    builder = StateGraph(PaperState)

    # ---------------- 1. 注册Nodes ----------------
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("audit", audit_node)
    builder.add_node("issue_review", issue_review_node)
    builder.add_node("revision", revision_node)
    builder.add_node("patch_review", patch_review_node)
    builder.add_node("apply", apply_node)
    builder.add_node("review", review_node)
    builder.add_node("prepare_report", prepare_report_node)
    builder.add_node("report", report_node)
    builder.add_edge("prepare_report", "report")

    # ---------------- 2. 入口 ----------------
    builder.add_edge(START, "orchestrator")

    # ---------------- 3. Orchestrator的条件路由 ----------------
    builder.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        {
            "audit": "audit",
            "revision": "revision",
            # Orchestrator说“需要人拍板”时，统一走问题闸门（列出问题问用户）
            "human_approval": "issue_review",
            "report": "prepare_report",
        },
    )

    # ---------------- 4. Audit之后 ----------------
    builder.add_conditional_edges(
        "audit",
        route_after_audit,
        {
            "issue_review": "issue_review",
            "revision": "revision",
            "report": "prepare_report",
        },
    )

    # ---------------- 5. 问题闸门之后 ----------------
    builder.add_conditional_edges(
        "issue_review",
        route_after_issue_review,
        {
            "revision": "revision",
            "report": "prepare_report",
        },
    )

    # ---------------- 6. 提补丁 → 补丁审阅 → 应用 ----------------
    builder.add_edge("revision", "patch_review")

    builder.add_conditional_edges(
        "patch_review",
        route_after_patch_review,
        {
            "apply": "apply",
            "report": "prepare_report",
        },
    )

    builder.add_edge("apply", "review")

    # ---------------- 7. 复核之后的闭环 ----------------
    builder.add_conditional_edges(
        "review",
        route_after_review,
        {
            "report": "prepare_report",
            "orchestrator": "orchestrator",
        },
    )

    # ---------------- 8. 报告结束 ----------------
    builder.add_edge("report", END)

    return builder.compile(checkpointer=checkpointer or build_checkpointer(settings))


def mermaid(graph: Any) -> str:
    """导出Graph的Mermaid结构（用于文档与调试）。"""

    return graph.get_graph().draw_mermaid()
