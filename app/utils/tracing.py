"""
轻量 Trace / 可观测性。

记录每个 Agent 的执行时间（latency）、重试次数（attempts）、
Token 消耗、成功 / 失败与错误信息。

输出三个地方：

1. 追加写入 data/runs/<run_id>/trace.jsonl（每行一个事件，方便后期分析）；
2. 提供 TraceRecorder.summary() 供 Report 使用；
3. 精简后写入 State 的 trace 字段。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import get_settings


def utc_now() -> str:
    """当前 UTC 时间（ISO 格式）。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TraceRecord:
    """一次 Agent 调用的观测记录。"""

    run_id: str
    agent: str
    action: str = "call"
    iteration: int = 0
    started_at: str = ""
    duration_ms: float = 0.0
    attempts: int = 1
    ok: bool = True
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_state(self) -> dict[str, Any]:
        """转成写进 State 的精简事件。"""

        event: dict[str, Any] = {
            "agent": self.agent,
            "action": self.action,
            "iteration": self.iteration,
            "duration_ms": round(self.duration_ms, 1),
            "attempts": self.attempts,
            "ok": self.ok,
        }
        if self.total_tokens:
            event.update(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
                total_tokens=self.total_tokens,
            )
        if self.error:
            event["error"] = self.error
        if self.extra:
            event["extra"] = self.extra
        return event


class TraceRecorder:
    """按 run_id 收集 trace 事件。"""

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path
        self._events: list[TraceRecord] = []
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            allowed = {item.name for item in fields(TraceRecord)}
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        record = json.loads(line)
                        self._events.append(TraceRecord(**{k: v for k, v in record.items() if k in allowed}))
                    except (ValueError, TypeError, AttributeError):
                        continue
            except OSError:
                pass

    # ---------- 记录 ----------

    def record(self, record: TraceRecord) -> TraceRecord:
        """记录一个事件，并追加写盘（写盘失败不影响主流程）。"""

        with self._lock:
            self._events.append(record)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                except OSError:
                    pass
        return record

    def events(self) -> list[TraceRecord]:
        with self._lock:
            return list(self._events)

    # ---------- 统计 ----------

    def summary(self) -> dict[str, Any]:
        """按 Agent 聚合的统计信息。"""

        events = self.events()
        per_agent: dict[str, dict[str, Any]] = {}
        for event in events:
            bucket = per_agent.setdefault(
                event.agent,
                {
                    "calls": 0,
                    "failures": 0,
                    "retries": 0,
                    "total_ms": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            bucket["calls"] += 1
            bucket["failures"] += 0 if event.ok else 1
            bucket["retries"] += max(0, event.attempts - 1)
            bucket["total_ms"] += event.duration_ms
            bucket["prompt_tokens"] += event.prompt_tokens
            bucket["completion_tokens"] += event.completion_tokens
            bucket["total_tokens"] += event.total_tokens

        for bucket in per_agent.values():
            bucket["total_ms"] = round(bucket["total_ms"], 1)

        totals = {
            "calls": sum(bucket["calls"] for bucket in per_agent.values()),
            "failures": sum(bucket["failures"] for bucket in per_agent.values()),
            "retries": sum(bucket["retries"] for bucket in per_agent.values()),
            "total_ms": round(sum(bucket["total_ms"] for bucket in per_agent.values()), 1),
            "total_tokens": sum(bucket["total_tokens"] for bucket in per_agent.values()),
        }

        return {
            "run_id": self.run_id,
            "per_agent": per_agent,
            "totals": totals,
            "trace_path": str(self.path) if self.path else "",
        }


_RECORDERS: dict[str, TraceRecorder] = {}
_RECORDERS_LOCK = threading.Lock()


def get_recorder(run_id: str, *, persist: bool = True) -> TraceRecorder:
    """获取（或创建）某个 run 的 TraceRecorder。"""

    key = run_id or "adhoc"
    with _RECORDERS_LOCK:
        recorder = _RECORDERS.get(key)
        if recorder is None:
            path = None
            if persist:
                settings = get_settings()
                path = settings.paths.run_dir(key) / "trace.jsonl"
            recorder = TraceRecorder(key, path)
            _RECORDERS[key] = recorder
        return recorder


def reset_recorders(run_id: str | None = None) -> None:
    """
    清理 recorder 注册表。

    传入 run_id 时只清掉这一个（多任务并发时必须这样，
    否则一个任务启动会清掉其它任务正在累积的统计）；
    不传时清空全部（测试用）。
    """

    with _RECORDERS_LOCK:
        if run_id is None:
            _RECORDERS.clear()
        else:
            _RECORDERS.pop(run_id or "adhoc", None)


def record_step(
    run_id: str,
    agent: str,
    action: str,
    *,
    iteration: int = 0,
    duration_ms: float = 0.0,
    ok: bool = True,
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录一条非 LLM 的流程事件（例如 Workflow 进入某个节点）。"""

    key = run_id or "adhoc"
    record = TraceRecord(
        run_id=key,
        agent=agent,
        action=action,
        iteration=iteration,
        started_at=utc_now(),
        duration_ms=duration_ms,
        ok=ok,
        error=error,
        extra=extra or {},
    )
    get_recorder(key).record(record)
    return record.to_state()


def stopwatch() -> tuple[float, Callable[[], float]]:
    """返回 (起点, 计算已过去毫秒数的函数)。"""

    start = time.perf_counter()
    return start, lambda: (time.perf_counter() - start) * 1000.0
