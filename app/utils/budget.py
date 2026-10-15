"""
成本与延迟预算。

三个硬指标（都可以在 .env 里调）：

1. **每任务 token 上限**：整次任务（含多轮）用掉多少 token；
2. **每轮 token 上限**：单轮（一次修改循环）的消耗；
3. **墙钟上限**：整次任务允许跑多久。

另外还有“每个 Agent 的调用次数上限”，用来兜住异常情况
（例如文档被切得太碎，Audit 一个分块一个分块地烧钱）。

用量从 ``data/runs/<run_id>/trace.jsonl`` 实时汇总，因此：

- 不需要额外的计数器，重启进程也不会丢；
- 观测到的数字就是报告里展示的数字。

超预算的处理方式是**降级而不是崩溃**：调用方捕获 BudgetExceeded 后
收尾当前工作（保留已完成的部分），并让流程尽快走到报告。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.utils.tracing import get_recorder, utc_now
from app.utils.call_policy import get_policy


class BudgetExceeded(RuntimeError):
    """超出预算。"""

    def __init__(self, reason: str, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot or {}


def start_run(state: Any) -> str:
    """记录任务开始时间（只在第一次进入 Workflow 时写）。"""

    started = state.get("run_started_at") or utc_now()
    return started


def _elapsed_seconds(started_at: str) -> float:
    if not started_at:
        return 0.0
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def usage(state: Any) -> dict[str, Any]:
    """当前用量（token / 耗时 / 各 Agent 调用次数）。"""

    settings = get_settings()
    run_id = state.get("run_id") or "adhoc"
    summary = get_recorder(run_id).summary()

    per_agent_calls = {
        agent: bucket.get("calls", 0) for agent, bucket in summary["per_agent"].items()
    }
    used_tokens = int(summary["totals"].get("total_tokens") or 0)
    started_at = state.get("run_started_at") or ""
    elapsed = _elapsed_seconds(started_at)
    policy = get_policy(run_id)
    if policy is not None:
        # Web 人工闸门可能等待数天，预算只计算实际后台执行时间。
        elapsed = policy.elapsed_seconds()
    turn_started_at = state.get("turn_started_at") or started_at
    turn_elapsed = _elapsed_seconds(turn_started_at)

    return {
        "used_tokens": used_tokens,
        "max_run_tokens": settings.budget.max_run_tokens,
        "max_turn_tokens": settings.budget.max_turn_tokens,
        "max_run_seconds": settings.budget.max_run_seconds,
        "max_calls_per_agent": settings.budget.max_calls_per_agent,
        "elapsed_seconds": round(elapsed, 1),
        "turn_elapsed_seconds": round(turn_elapsed, 1),
        "calls_total": int(summary["totals"].get("calls") or 0),
        "retries": int(summary["totals"].get("retries") or 0),
        "failures": int(summary["totals"].get("failures") or 0),
        "calls_per_agent": per_agent_calls,
        "turn": int(state.get("turn", 1)),
        "exceeded": False,
        "reason": "",
    }


def check(state: Any, *, agent: str = "", stage: str = "") -> dict[str, Any]:
    """
    在发起下一次模型调用前检查预算。

    超限抛 :class:`BudgetExceeded`（携带当时的用量快照）。
    """

    snapshot = usage(state)
    settings = get_settings()
    budget = settings.budget

    reason = ""
    if budget.max_run_tokens and snapshot["used_tokens"] >= budget.max_run_tokens:
        reason = f"任务 token 预算已用尽（{snapshot['used_tokens']}/{budget.max_run_tokens}）"
    elif budget.max_turn_tokens and snapshot["used_tokens"] >= budget.max_turn_tokens:
        reason = f"本轮 token 预算已用尽（{snapshot['used_tokens']}/{budget.max_turn_tokens}）"
    elif budget.max_run_seconds and snapshot["elapsed_seconds"] >= budget.max_run_seconds:
        reason = (
            f"任务时间预算已用尽（{snapshot['elapsed_seconds']}s / {budget.max_run_seconds:.0f}s）"
        )
    elif agent and budget.max_calls_per_agent:
        calls = snapshot["calls_per_agent"].get(agent, 0)
        if calls >= budget.max_calls_per_agent:
            reason = f"{agent} 调用次数已达上限（{calls}/{budget.max_calls_per_agent}）"

    if reason:
        snapshot["exceeded"] = True
        snapshot["reason"] = reason
        if stage:
            snapshot["stage"] = stage
        raise BudgetExceeded(reason, snapshot)

    if stage:
        snapshot["stage"] = stage
    return snapshot


def exceeded_snapshot(state: Any, exc: BudgetExceeded) -> dict[str, Any]:
    """把一次超预算转成写进 State 的记录。"""

    snapshot = dict(exc.snapshot or usage(state))
    snapshot["exceeded"] = True
    snapshot["reason"] = exc.reason
    return snapshot
