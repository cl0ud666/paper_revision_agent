"""
服务层：把 LangGraph Workflow 包装成“可被 Web 服务管理的任务”。

这一层刻意不依赖任何 Web 框架（FastAPI / Flask 都只是它的薄适配层），
因此换框架、加 CLI、做定时任务，业务逻辑都不用改。

关键设计：

1. **任务是后台线程跑的**：一次论文修改要调用模型几十秒到几分钟，
   HTTP 请求不能阻塞；提交后立刻返回 run_id，前端轮询或订阅 SSE 看进度。
2. **进度来自 trace.jsonl 增量读取**：不用改 Workflow，
   任何进程都能看到每个 Agent 的实时进展（耗时 / 重试 / Token）。
3. **人工确认是一个可恢复的资源**：状态停在 awaiting_human，
   前端刷新、换浏览器、甚至服务重启（配了 SQLite Checkpoint）都能接着确认。
4. **状态落盘**：每次状态变化写 data/runs/<run_id>/session.json，
   重启后能恢复出任务列表与等待确认的问题清单。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agents.revision import all_applied_edits, all_failed_edits
from app.config import get_settings
from app.utils.tracing import utc_now
from app.workflow.harness import Harness, new_run_id
from app.workflow.interaction import prepare_response

# ---------- 任务状态 ----------
STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_AWAITING = "awaiting_human"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED}

MAX_EVENTS = 2000
MAX_LOGS = 500

# 事件标签（前端时间线用中文展示）
AGENT_LABELS = {
    "orchestrator": "Orchestrator 规划",
    "audit": "Audit 审查",
    "revision": "Revision 修改",
    "review": "Review 复核",
    "judge": "Judge 评分",
    "workflow": "流程",
    "system": "系统",
}


class RunNotFound(KeyError):
    """任务不存在。"""


class RunStateError(RuntimeError):
    """任务状态不允许当前操作（例如重复提交、没有等待确认）。"""


@dataclass
class RunOptions:
    """一次任务的输入参数。"""

    user_request: str
    document: str = ""
    document_name: str = ""
    max_iterations: int | None = None
    auto_approve: bool = False
    constraints: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    source_docx: str = ""

    def to_json(self, *, include_document: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "user_request": self.user_request,
            "document_name": self.document_name,
            "max_iterations": self.max_iterations,
            "auto_approve": self.auto_approve,
            "constraints": list(self.constraints),
            "rules": list(self.rules),
            "source_docx": self.source_docx,
            "document_chars": len(self.document or ""),
        }
        if include_document:
            payload["document"] = self.document
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RunOptions":
        return cls(
            user_request=payload.get("user_request", ""),
            document=payload.get("document", ""),
            document_name=payload.get("document_name", ""),
            max_iterations=payload.get("max_iterations"),
            auto_approve=bool(payload.get("auto_approve")),
            constraints=list(payload.get("constraints") or []),
            rules=list(payload.get("rules") or []),
            source_docx=str(payload.get("source_docx") or ""),
        )


class RunSession:
    """单个任务：后台线程执行 + 状态可查询 + 可恢复。"""

    def __init__(self, run_id: str, options: RunOptions, *, persistent: bool = True) -> None:
        self.settings = get_settings()
        self.run_id = run_id
        self.options = options
        self.thread_id = f"thread-{run_id}"
        self.persistent = persistent

        self.run_dir: Path = self.settings.paths.run_dir(run_id)
        self.session_path = self.run_dir / "session.json"
        self.trace_path = self.run_dir / "trace.jsonl"
        self.state_path = self.run_dir / "state.json"

        self.status = STATUS_CREATED
        self.stage = "created"
        self.error = ""
        self.pending: dict[str, Any] = {}
        self.created_at = utc_now()
        self.updated_at = self.created_at
        self.finished_at = ""

        self._lock = threading.RLock()
        self._harness: Harness | None = None
        self._result: Any = None
        self._state_cache: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._file_cursor = 0
        self._logs: list[str] = []
        self._thread: threading.Thread | None = None
        self._limiter: threading.BoundedSemaphore | None = None
        self._slot_held = False
        self._slot_generation = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """提交任务（立刻返回，实际执行在后台线程）。"""

        with self._lock:
            if self.status == STATUS_RUNNING:
                raise RunStateError("该任务正在运行中")
            if self.status not in {STATUS_CREATED, STATUS_FAILED}:
                raise RunStateError(f"任务状态为 {self.status}，不能重新开始")
            if not (self.options.document or "").strip():
                raise RunStateError("论文内容为空，无法开始")

            self._append_event(
                {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "start",
                    "label": "任务已提交",
                    "ok": True,
                }
            )
            target = self._entry_auto if self.options.auto_approve else self._entry_start
            self._spawn(target)

    def resume(self, payload: dict[str, Any]) -> None:
        """用户确认后继续执行（同样在后台线程）。"""

        with self._lock:
            if self.status != STATUS_AWAITING:
                raise RunStateError(f"任务状态为 {self.status}，当前没有等待人工确认的环节")
            payload = prepare_response(self.pending, payload)
            approved = payload.get("approved_issue_ids")
            count = len(approved) if isinstance(approved, list) else "全部"
            self._append_event(
                {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "resume",
                    "label": f"用户已确认修改范围（{count} 个问题）",
                    "ok": True,
                }
            )
            self._spawn(lambda: self._entry_resume(payload))

    def follow_up(
        self,
        instruction: str,
        *,
        constraints: list[str] | None = None,
    ) -> None:
        """
        多轮会话：在当前文档基础上继续提新要求。

        例如第一轮“全面检查并修改”，第二轮“把摘要再学术一点”。
        已接受的改动、被拒绝的改动、审计结果都会保留在状态里。
        """

        with self._lock:
            if self.status == STATUS_RUNNING:
                raise RunStateError("当前任务还在运行，请等它停下来")
            if self.status == STATUS_AWAITING:
                raise RunStateError("当前任务在等待你确认补丁，请先处理补丁清单")
            if not (instruction or "").strip():
                raise RunStateError("请提供本轮的新要求")

            next_turn = int(self._state().get("turn", 1)) + 1
            self._append_event(
                {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "follow_up",
                    "label": f"第 {next_turn} 轮要求：{instruction[:30]}",
                    "ok": True,
                }
            )
            self._spawn(lambda: self._entry_follow_up(instruction, constraints))

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_RUNNING

    # ------------------------------------------------------------ 执行入口

    def _spawn(self, target: Callable[[], None]) -> None:
        if self._limiter is not None and not self._limiter.acquire(blocking=False):
            raise RunStateError("并发任务已达上限，请等当前任务完成后再继续")
        self._slot_held = self._limiter is not None
        self._slot_generation += 1
        generation = self._slot_generation
        old_status, old_stage = self.status, self.stage
        self.status = STATUS_RUNNING
        self.stage = "planning"
        self.updated_at = utc_now()

        def runner() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
                self._fail(f"{type(exc).__name__}: {exc}")
            finally:
                with self._lock:
                    self._release_slot(generation)

        self._thread = threading.Thread(target=runner, name=f"run-{self.run_id}", daemon=True)
        try:
            self._save()
            self._thread.start()
        except Exception:
            self._release_slot()
            self.status, self.stage = old_status, old_stage
            self._save()
            raise

    def _release_slot(self, generation: int | None = None) -> None:
        if generation is not None and generation != self._slot_generation:
            return
        if self._slot_held:
            self._slot_held = False
            self._limiter.release()

    def _harness_for_run(self) -> Harness:
        if self._harness is None:
            self._harness = Harness(
                thread_id=self.thread_id,
                auto_approve=False,
                auto_approve_constraints=list(self.options.constraints),
                logger=self._log,
            )
            # 重启后继续：run_id / thread_id 都是确定的，可以直接恢复
            self._harness.run_id = self.run_id
        return self._harness

    def _entry_start(self) -> None:
        harness = self._harness_for_run()
        result = harness.start(
            self.options.user_request,
            self.options.document,
            run_id=self.run_id,
            max_iterations=self.options.max_iterations,
            rules=self.options.rules,
            source_docx=self.options.source_docx,
        )
        self._apply_result(result)

    def _entry_auto(self) -> None:
        harness = self._harness_for_run()
        result = harness.run_to_completion(
            self.options.user_request,
            self.options.document,
            run_id=self.run_id,
            max_iterations=self.options.max_iterations,
            rules=self.options.rules,
            source_docx=self.options.source_docx,
        )
        self._apply_result(result)

    def _entry_resume(self, payload: dict[str, Any]) -> None:
        harness = self._harness_for_run()
        result = harness.resume(payload)
        self._apply_result(result)

    def _entry_follow_up(self, instruction: str, constraints: list[str] | None) -> None:
        harness = self._harness_for_run()
        result = harness.follow_up(instruction, constraints=constraints)
        self._apply_result(result)

    def _apply_result(self, result: Any) -> None:
        with self._lock:
            self._result = result
            state = dict(result.state or {})
            review_status = (state.get("review_result") or {}).get("status", "")

            if result.status == "interrupted":
                new_status, new_stage = STATUS_AWAITING, "awaiting_human"
                pending = deepcopy(result.pending_request or {})
                event = {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "awaiting_human",
                    "label": f"等待人工确认：{len(pending.get('issues') or [])} 个待确认问题",
                    "ok": True,
                }
            elif result.status == "completed":
                new_status, new_stage = STATUS_COMPLETED, "done"
                pending = {}
                self.finished_at = utc_now()
                event = {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "completed",
                    "label": f"任务完成（Review={str(review_status).upper() or '未执行'}）",
                    "ok": True,
                }
            else:
                self._fail(result.error or "运行失败")
                return

            self.updated_at = utc_now()
            self._state_cache = state
            # 落一份状态快照：服务重启后还能重建“等待确认”的问题清单
            self._write_state_snapshot(state)
            # 先落盘再更新内存：这样任何读到新状态的读者，
            # 都能保证磁盘上的 session.json 已经同步（避免读到旧状态）
            self._write_session(new_status, new_stage, self.error, pending=pending)
            self._release_slot()
            self.status = new_status
            self.stage = new_stage
            self.pending = pending
            self._append_event(event)

    def _fail(self, message: str) -> None:
        with self._lock:
            self.error = message
            self.finished_at = utc_now()
            self.updated_at = self.finished_at
            self._release_slot()
            self.status = STATUS_FAILED
            self.stage = "failed"
            try:
                self._write_session(STATUS_FAILED, "failed", message)
            except RunStateError as exc:
                self.error = f"{message}；{exc}"
            self._append_event(
                {
                    "type": "lifecycle",
                    "agent": "system",
                    "action": "failed",
                    "label": f"任务失败：{message}",
                    "ok": False,
                }
            )

    # ------------------------------------------------------------ 状态查询

    def snapshot(self) -> dict[str, Any]:
        """给前端用的任务快照（不含大段正文，正文走单独接口）。"""

        self._ingest_events()
        state = self._state()
        review = state.get("review_result") or {}

        with self._lock:
            return {
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "status": self.status,
                "stage": self._effective_stage(),
                "error": self.error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "user_request": self.options.user_request,
                "document_name": self.options.document_name,
                "document_chars": len(self.options.document or ""),
                "max_iterations": self.options.max_iterations
                or self.settings.workflow.max_iterations,
                "iteration": int(state.get("iteration", 0)),
                "plan": state.get("plan") or [],
                "plan_kind": state.get("plan_kind", ""),
                "plan_reason": state.get("plan_reason", ""),
                "focus_areas": state.get("focus_areas") or [],
                "issues": _compact_issues(state.get("audit_findings") or []),
                "approved_issue_ids": state.get("approved_issue_ids") or [],
                "constraints": state.get("human_constraints") or [],
                "human_decision": state.get("human_decision", ""),
                "revision_summary": state.get("revision_summary", ""),
                "edits": _compact_edits(all_applied_edits(state)),
                "failed_edits": _compact_edits(all_failed_edits(state), failed=True),
                "review_status": review.get("status", ""),
                "review_summary": review.get("summary", ""),
                "dimension_scores": review.get("dimension_scores") or {},
                "checks": review.get("checks") or [],
                "remaining_issues": _compact_issues(review.get("remaining_issues") or []),
                # ---- 补丁（逐条接受/拒绝的清单）----
                "patches": _compact_patches(state.get("proposed_patches") or []),
                "decidable_patch_ids": [
                    patch.get("patch_id")
                    for patch in (state.get("proposed_patches") or [])
                    if patch.get("status") == "proposed"
                ],
                "rejected_patches": _compact_patches(state.get("rejected_patches") or []),
                "conflict_patches": _compact_patches(state.get("conflict_patches") or []),
                "patch_stats": state.get("patch_stats") or {},
                # ---- 用户要求与决定（带编号，可撤改）----
                "rules": [
                    {
                        "id": rule.get("id"),
                        "kind": rule.get("kind"),
                        "text": rule.get("text"),
                        "status": rule.get("status", "active"),
                        "created_turn": rule.get("created_turn", 1),
                    }
                    for rule in (state.get("user_rules") or [])
                ],
                # ---- 多轮与预算 ----
                "turn": int(state.get("turn", 1)),
                "turns": [
                    {
                        "turn": entry.get("turn"),
                        "instruction": entry.get("instruction", ""),
                        "summary": entry.get("summary", ""),
                    }
                    for entry in (state.get("turns") or [])
                ],
                "budget": self._budget(state),
                "trace_summary": self.trace_summary(),
                "has_report": bool(state.get("report_path")) or self._report_path().exists(),
                "has_document": bool(state.get("current_document")),
                "pending": self.pending if self.status == STATUS_AWAITING else {},
                "logs_tail": self._logs[-20:],
                "event_cursor": len(self._events),
            }

    def _budget(self, state: dict[str, Any]) -> dict[str, Any]:
        """预算快照（用量 + 上限），供界面展示。"""

        from app.utils.budget import usage as budget_usage

        try:
            return budget_usage(state)
        except Exception:  # noqa: BLE001
            return state.get("budget") or {}

    def events(self, cursor: int = 0) -> dict[str, Any]:
        """增量事件（轮询与 SSE 共用）。"""

        self._ingest_events()
        with self._lock:
            cursor = max(0, int(cursor or 0))
            return {
                "run_id": self.run_id,
                "cursor": len(self._events),
                "events": self._events[cursor:],
                "status": self.status,
                "stage": self._effective_stage(),
                "iteration": int(self._state().get("iteration", 0)),
            }

    def trace_summary(self) -> dict[str, Any]:
        """从 trace 文件实时汇总（不依赖进程内状态，重启后依然可用）。"""

        summary = _summarize_trace_file(self.trace_path)
        if summary["totals"]["calls"]:
            return summary
        return self._state().get("trace_summary") or summary

    def report_text(self) -> str:
        with self._lock:
            text = self._state().get("final_report") or ""
        if text:
            return text
        path = self._report_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def document_text(self) -> str:
        path = self.run_dir / "revised_document.md"
        if self.status == STATUS_COMPLETED and path.exists():
            return path.read_text(encoding="utf-8")
        with self._lock:
            return self._state().get("current_document") or self.options.document or ""

    def original_text(self) -> str:
        path = self.run_dir / "original_document.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return self.options.document or ""

    def to_json(self) -> dict[str, Any]:
        """任务列表用的精简信息。"""

        state = self._state()
        review = state.get("review_result") or {}
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self._effective_stage(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_request": self.options.user_request,
            "document_name": self.options.document_name,
            "iteration": int(state.get("iteration", 0)),
            "review_status": review.get("status", ""),
            "has_report": bool(state.get("report_path")) or self._report_path().exists(),
        }

    # ------------------------------------------------------------ 内部工具

    def _effective_stage(self) -> str:
        if self.status in {STATUS_COMPLETED, STATUS_FAILED, STATUS_AWAITING}:
            return self.stage
        for event in reversed(self._events):  # 运行中：用最后一个 Agent 事件推断阶段
            if event.get("type") == "agent":
                return str(event.get("agent") or "running")
        return self.stage

    def _report_path(self) -> Path:
        return self.run_dir / "report.md"

    def _log(self, message: str) -> None:
        """Harness 的日志回调（只留尾部，出错时用来排查）。"""

        try:
            with self._lock:
                self._logs.append(str(message))
                if len(self._logs) > MAX_LOGS:
                    del self._logs[: len(self._logs) - MAX_LOGS]
        except Exception:  # noqa: BLE001
            pass

    def _append_event(self, event: dict[str, Any]) -> None:
        item = dict(event)
        item.setdefault("at", utc_now())
        with self._lock:
            self._events.append(item)
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]

    def _ingest_events(self) -> None:
        """把 trace.jsonl 里新增的行转成前端事件。"""

        try:
            if not self.trace_path.exists():
                return
            lines = self.trace_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        with self._lock:
            pending = lines[self._file_cursor :]
            self._file_cursor = len(lines)
            for line in pending:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._events.append(_to_event(record))
            if len(self._events) > MAX_EVENTS:
                del self._events[: len(self._events) - MAX_EVENTS]

    def _state(self) -> dict[str, Any]:
        with self._lock:
            if self._result is not None:
                return dict(self._result.state or {})
            if self._state_cache is None:
                self._state_cache = _read_json(self.state_path) or {}
            return self._state_cache

    def _save(self) -> None:
        self._write_session(self.status, self.stage, self.error)

    def _write_session(
        self, status: str, stage: str, error: str, *, pending: dict | None = None
    ) -> None:
        if not self.persistent:
            return
        payload = {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "status": status,
            "stage": stage,
            "error": error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "options": self.options.to_json(),
            "pending_request": deepcopy(pending if pending is not None else self.pending)
            if status == STATUS_AWAITING
            else {},
        }
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.session_path, payload)
        except OSError as exc:
            raise RunStateError("任务状态持久化失败") from exc

    def _write_state_snapshot(self, state: dict[str, Any]) -> None:
        """把当前 State 存盘（剔除体积较大的 trace 字段）。"""

        if not self.persistent or not state:
            return
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            payload = {key: value for key, value in state.items() if key != "trace"}
            _atomic_json(self.state_path, payload)
        except OSError as exc:
            raise RunStateError("Workflow 状态快照持久化失败") from exc


class RunManager:
    """任务注册表：创建、查询、列表，以及从磁盘恢复历史任务。"""

    def __init__(self, *, max_concurrent: int | None = None) -> None:
        self.settings = get_settings()
        self.max_concurrent = (
            max_concurrent
            if max_concurrent is not None
            else self.settings.server.max_concurrent_runs
        )
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent 必须大于 0")
        self._limiter = threading.BoundedSemaphore(self.max_concurrent)
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.RLock()

    # ---------- 查询 ----------

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for session in self._sessions.values() if session.is_active)

    def get(self, run_id: str) -> RunSession:
        """取任务：内存里没有就从 data/runs/<run_id>/session.json 恢复。"""

        with self._lock:
            session = self._sessions.get(run_id)
            if session is not None:
                return session

        payload = _read_json(self.settings.paths.run_dir(run_id) / "session.json")
        if not payload:
            raise RunNotFound(run_id)

        session = RunSession(run_id, RunOptions.from_json(payload.get("options") or {}))
        session.status = payload.get("status", STATUS_CREATED)
        session.stage = payload.get("stage", "")
        session.error = payload.get("error", "")
        session.created_at = payload.get("created_at", session.created_at)
        session.updated_at = payload.get("updated_at", session.updated_at)
        session.finished_at = payload.get("finished_at", "")

        if session.status == STATUS_AWAITING:
            # 原样恢复实际请求，包括 defaults、选项和分组补丁 ID。
            session.pending = deepcopy(payload.get("pending_request") or {})
            if not session.pending:
                # 兼容旧 session：读取真正的 Checkpoint interrupt，禁止猜闸门。
                session.pending = session._harness_for_run().current_pending_request()
                if not session.pending:
                    raise RunStateError("暂停任务缺少持久化请求和 Checkpoint，无法安全恢复")
                session._save()

        with self._lock:
            if run_id in self._sessions:
                return self._sessions[run_id]
            session._limiter = self._limiter
            self._sessions[run_id] = session
        return session

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """任务列表：内存中的 + 磁盘上的历史任务。"""

        with self._lock:
            known = dict(self._sessions)

        runs_dir = self.settings.paths.runs_dir
        items: list[dict[str, Any]] = []
        if not runs_dir.exists():
            return items

        directories = [
            path for path in runs_dir.iterdir() if path.is_dir() and path.name != "evals"
        ]
        directories.sort(key=lambda path: path.stat().st_mtime, reverse=True)

        for path in directories[:limit]:
            session = known.get(path.name)
            if session is not None:
                items.append(session.to_json())
                continue

            payload = _read_json(path / "session.json")
            if payload:
                options = payload.get("options") or {}
                items.append(
                    {
                        "run_id": path.name,
                        "status": payload.get("status", STATUS_CREATED),
                        "stage": payload.get("stage", ""),
                        "created_at": payload.get("created_at", ""),
                        "updated_at": payload.get("updated_at", ""),
                        "user_request": options.get("user_request", ""),
                        "document_name": options.get("document_name", ""),
                        "iteration": 0,
                        "review_status": "",
                        "has_report": (path / "report.md").exists(),
                    }
                )
                continue

            # CLI 直接跑的 run（没有 session.json）
            state = _read_json(path / "state.json") or {}
            items.append(
                {
                    "run_id": path.name,
                    "status": STATUS_COMPLETED,
                    "stage": "done",
                    "created_at": "",
                    "updated_at": "",
                    "user_request": state.get("user_request", ""),
                    "document_name": "",
                    "iteration": int(state.get("iteration", 0)),
                    "review_status": (state.get("review_result") or {}).get("status", ""),
                    "has_report": (path / "report.md").exists(),
                }
            )
        return items

    # ---------- 创建 ----------

    def create(self, options: RunOptions, *, run_id: str | None = None) -> RunSession:
        if not (options.user_request or "").strip():
            raise RunStateError("缺少任务描述")
        if not (options.document or "").strip():
            raise RunStateError("论文内容为空")
        session = RunSession(run_id if run_id is not None else new_run_id("paper"), options)
        with self._lock:
            if self.active_count() >= self.max_concurrent:
                raise RunStateError(f"并发任务已达上限（{self.max_concurrent}），请稍后提交")
            if session.run_id in self._sessions or session.run_dir.exists():
                raise RunStateError("run_id 已存在，请使用新 ID 或恢复原任务")
            # mkdir 是跨线程/进程的原子目录预留，避免同名任务覆盖。
            session.run_dir.mkdir(parents=True, exist_ok=False)
            session._limiter = self._limiter
            session._save()
            self._sessions[session.run_id] = session
        return session


# ------------------------------------------------------------------ 工具函数


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录临时文件 + 原子替换，读者不会看到半个 JSON。"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _to_event(record: dict[str, Any]) -> dict[str, Any]:
    """trace.jsonl 的一行 -> 前端事件。"""

    agent = str(record.get("agent") or "")
    action = str(record.get("action") or "")
    return {
        "type": "agent",
        "agent": agent,
        "action": action,
        "label": f"{AGENT_LABELS.get(agent, agent)} · {action}",
        "iteration": record.get("iteration", 0),
        "duration_ms": round(float(record.get("duration_ms") or 0)),
        "attempts": int(record.get("attempts") or 1),
        "ok": bool(record.get("ok", True)),
        "tokens": int(record.get("total_tokens") or 0),
        "error": record.get("error", ""),
        "at": record.get("started_at", ""),
    }


def _compact_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "category", "severity", "location", "problem", "suggestion", "approved")
    compact: list[dict[str, Any]] = []
    for issue in issues:
        compact.append({key: issue.get(key) for key in keys if issue.get(key) is not None})
    return compact


def _compact_edits(edits: list[dict[str, Any]], *, failed: bool = False) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for edit in edits:
        item: dict[str, Any] = {
            "issue_id": edit.get("issue_id"),
            "round": edit.get("round"),
            "strategy": edit.get("strategy"),
            "rationale": edit.get("rationale"),
        }
        if failed:
            item["reason"] = edit.get("reason")
            item["find"] = _clip(edit.get("find"))
        else:
            item["original"] = _clip(edit.get("original"))
            item["revised"] = _clip(edit.get("revised"))
        compact.append(item)
    return compact


def _compact_patches(patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """补丁清单（给界面 / 命令行看，含理由，便于逐条判断）。"""

    keys = (
        "patch_id",
        "issue_id",
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
        "round",
        "conflict_reason",
        "warnings",
        "decided_by",
        "decision_note",
    )
    compact: list[dict[str, Any]] = []
    for patch in patches:
        compact.append({key: patch.get(key) for key in keys if patch.get(key) is not None})
    return compact


def _clip(value: Any, limit: int = 600) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize_trace_file(path: Path) -> dict[str, Any]:
    """从 trace.jsonl 实时汇总（进程重启后也能给出完整统计）。"""

    per_agent: dict[str, dict[str, Any]] = {}
    empty = {
        "per_agent": {},
        "totals": {
            "calls": 0,
            "failures": 0,
            "retries": 0,
            "total_ms": 0.0,
            "total_tokens": 0,
        },
        "trace_path": str(path),
    }

    try:
        if not path.exists():
            return empty
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return empty

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        agent = str(record.get("agent") or "unknown")
        bucket = per_agent.setdefault(
            agent,
            {"calls": 0, "failures": 0, "retries": 0, "total_ms": 0.0, "total_tokens": 0},
        )
        bucket["calls"] += 1
        bucket["failures"] += 0 if record.get("ok", True) else 1
        bucket["retries"] += max(0, int(record.get("attempts") or 1) - 1)
        bucket["total_ms"] += float(record.get("duration_ms") or 0)
        bucket["total_tokens"] += int(record.get("total_tokens") or 0)

    for bucket in per_agent.values():
        bucket["total_ms"] = round(bucket["total_ms"], 1)

    return {
        "per_agent": per_agent,
        "totals": {
            "calls": sum(bucket["calls"] for bucket in per_agent.values()),
            "failures": sum(bucket["failures"] for bucket in per_agent.values()),
            "retries": sum(bucket["retries"] for bucket in per_agent.values()),
            "total_ms": round(sum(bucket["total_ms"] for bucket in per_agent.values()), 1),
            "total_tokens": sum(bucket["total_tokens"] for bucket in per_agent.values()),
        },
        "trace_path": str(path),
    }
