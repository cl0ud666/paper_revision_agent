"""
异常隔离与熔断（Agent Harness 的可靠性部分）。

三个能力：

1. Timeout：单次 Agent 调用超时后放弃，避免长任务卡死；
2. Retry：指数退避 + 抖动重试；
3. Circuit Breaker：每个 Agent 独立统计失败，连续失败到阈值后快速失败，
   避免“坏掉的 Agent 被反复调用”把时间和 Token 全部烧光。

注意：熔断是**按 Agent** 生效的，Orchestrator 熔断不影响 Audit。
"""

from __future__ import annotations

import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from app.config import get_settings
from app.utils.tracing import TraceRecord, utc_now

T = TypeVar("T")


class AgentError(RuntimeError):
    """Agent 执行失败的基类。"""


class CircuitOpenError(AgentError):
    """熔断器打开，快速失败。"""


class AgentTimeoutError(AgentError):
    """Agent 调用超时。"""


@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    jitter: float = 0.25
    retry_on_timeout: bool = False

    def delay_for(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """
        第 attempt 次失败后应等待的秒数（attempt 从 1 开始）。
        """

        raw = min(self.base_delay * math.pow(2, max(0, attempt - 1)), self.max_delay)
        factor = 1.0 - self.jitter + 2.0 * self.jitter * rand()
        return max(0.0, raw * factor)


@dataclass
class CircuitBreaker:
    """按 Agent 的熔断器（closed / open / half_open 三态）。"""

    name: str
    failure_threshold: int = 4
    recovery_timeout: float = 90.0
    failures: int = 0
    opened_at: float | None = None
    state: str = "closed"
    clock: Callable[[], float] = time.monotonic

    def check(self) -> None:
        """调用前检查；熔断打开时抛 CircuitOpenError。"""

        if self.state != "open":
            return
        opened_at = self.opened_at or 0.0
        if self.clock() - opened_at >= self.recovery_timeout:
            # 冷却结束，允许一次试探（half-open）
            self.state = "half_open"
            return
        remaining = self.recovery_timeout - (self.clock() - opened_at)
        raise CircuitOpenError(
            f"{self.name} 熔断中，约 {remaining:.0f}s 后重试（连续失败 {self.failures} 次）"
        )

    def on_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.state = "closed"

    def on_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = self.clock()

    def reset(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.state = "closed"

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "state": self.state,
            "failures": self.failures,
            "failure_threshold": self.failure_threshold,
        }


@dataclass
class AgentGuard:
    """
    把 Timeout + Retry + CircuitBreaker 组合成统一的调用入口。

    线程安全说明：Guard 是**按 Agent 共享的单例**（``get_guard`` 缓存），
    而 Audit 的分块会并发调用同一个 Guard。熔断器的 ``failures += 1``
    是"读-改-写"，并发下会丢计数（24 个分块同时失败只记 1 次），
    所以这里用一把锁把熔断器的状态变更串起来。

    锁只保护**状态变更**，不包住 ``fn`` 调用，也不包住重试等待，
    否则并发会退化成串行。
    """

    name: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("agent"))
    default_timeout: float | None = 180.0
    sleep: Callable[[float], None] = time.sleep
    on_event: Callable[[str], None] | None = None
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def run(
        self,
        fn: Callable[[], T],
        *,
        action: str = "call",
        timeout: float | None = None,
        recorder: Any | None = None,
        run_id: str = "",
        iteration: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> T:
        """
        执行 fn，带重试、超时与熔断。

        成功返回 fn 的返回值，失败抛出最后一次的异常。
        """

        effective_timeout = self.default_timeout if timeout is None else timeout
        started = time.perf_counter()
        attempts = 0
        last_error: BaseException | None = None

        for attempt in range(1, self.retry.max_attempts + 1):
            attempts = attempt
            try:
                self._breaker_check()
            except CircuitOpenError as exc:
                last_error = exc
                break

            try:
                value = _call_with_timeout(fn, effective_timeout)
            except AgentTimeoutError as exc:
                last_error = exc
                self._breaker_failure()
                self._notify(f"{self.name} 超时（{effective_timeout}s）")
                if not self.retry.retry_on_timeout:
                    break
            except Exception as exc:  # noqa: BLE001 - 统一治理所有异常
                last_error = exc
                self._breaker_failure()
                self._notify(f"{self.name} 第 {attempt} 次失败：{exc}")
                if attempt >= self.retry.max_attempts:
                    break
                self.sleep(self.retry.delay_for(attempt))
                continue
            else:
                self._breaker_success()
                self._trace(
                    recorder,
                    run_id=run_id,
                    action=action,
                    iteration=iteration,
                    started=started,
                    attempts=attempts,
                    ok=True,
                    error="",
                    value=value,
                    extra=extra,
                )
                self._notify(self._success_message(action, value, attempts, started))
                return value

        self._trace(
            recorder,
            run_id=run_id,
            action=action,
            iteration=iteration,
            started=started,
            attempts=attempts,
            ok=False,
            error=str(last_error),
            value=None,
            extra=extra,
        )
        self._notify(f"{self.name} 最终失败（尝试 {attempts} 次）：{last_error}")
        if last_error is None:
            raise AgentError(f"{self.name} 执行失败")
        raise last_error

    # ---------- 内部 ----------

    def _breaker_check(self) -> None:
        with self._lock:
            self.breaker.check()

    def _breaker_success(self) -> None:
        with self._lock:
            self.breaker.on_success()

    def _breaker_failure(self) -> None:
        with self._lock:
            self.breaker.on_failure()

    def _notify(self, message: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(message)
            except Exception:  # noqa: BLE001
                pass

    def _success_message(
        self, action: str, value: Any, attempts: int, started: float
    ) -> str:
        """成功时的输出：Agent、动作、耗时、Token、以及返回内容的预览。"""

        duration_ms = (time.perf_counter() - started) * 1000.0
        usage = getattr(value, "usage", None)
        tokens = getattr(usage, "total_tokens", 0) if usage else 0
        parts = [f"[{self.name}] {action} 完成：{duration_ms:.0f}ms"]
        if tokens:
            parts.append(f"{tokens} tokens")
        if attempts > 1:
            parts.append(f"第 {attempts} 次尝试成功")
        line = " · ".join(parts)

        text = getattr(value, "text", None) or getattr(value, "content", None)
        if isinstance(text, str) and text.strip():
            preview = " ".join(text.split())[:160]
            line += f"\n    → {preview}"
        return line

    def _trace(
        self,
        recorder: Any | None,
        *,
        run_id: str,
        action: str,
        iteration: int,
        started: float,
        attempts: int,
        ok: bool,
        error: str,
        value: Any,
        extra: dict[str, Any] | None,
    ) -> None:
        if recorder is None:
            return
        usage = getattr(value, "usage", None)
        model = getattr(value, "model", "") or ""
        record = TraceRecord(
            run_id=run_id or getattr(recorder, "run_id", "adhoc"),
            agent=self.name,
            action=action,
            iteration=iteration,
            started_at=utc_now(),
            duration_ms=(time.perf_counter() - started) * 1000.0,
            attempts=attempts,
            ok=ok,
            error=error,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
            model=model,
            extra=extra or {},
        )
        recorder.record(record)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.breaker.snapshot()


def _call_with_timeout(fn: Callable[[], T], timeout: float | None) -> T:
    """在独立线程里执行 fn，超时抛 AgentTimeoutError。"""

    if not timeout or timeout <= 0:
        return fn()

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-guard")
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as exc:
        future.cancel()
        raise AgentTimeoutError(f"调用超过 {timeout}s 未返回") from exc
    finally:
        # 不等待卡住的线程，避免 Harness 自己被拖死
        executor.shutdown(wait=False, cancel_futures=True)


_GUARDS: dict[str, AgentGuard] = {}
_GUARDS_LOCK = threading.Lock()


def get_guard(agent: str, *, on_event: Callable[[str], None] | None = None) -> AgentGuard:
    """获取（或创建）某个 Agent 的 Guard。"""

    with _GUARDS_LOCK:
        guard = _GUARDS.get(agent)
        if guard is None:
            settings = get_settings()
            guard = AgentGuard(
                name=agent,
                retry=RetryPolicy(
                    max_attempts=settings.retry.max_attempts,
                    base_delay=settings.retry.base_delay,
                    max_delay=settings.retry.max_delay,
                    jitter=settings.retry.jitter,
                ),
                breaker=CircuitBreaker(
                    name=agent,
                    failure_threshold=settings.circuit.failure_threshold,
                    recovery_timeout=settings.circuit.recovery_timeout,
                ),
                default_timeout=settings.workflow.agent_timeout,
                on_event=on_event,
            )
            _GUARDS[agent] = guard
        elif on_event is not None:
            guard.on_event = on_event
        return guard


def reset_guards() -> None:
    """清空 Guard 注册表（测试用）。"""

    with _GUARDS_LOCK:
        _GUARDS.clear()


def guard_snapshots() -> list[dict[str, Any]]:
    """所有 Agent 的熔断器状态（用于报告与调试）。"""

    with _GUARDS_LOCK:
        return [guard.snapshot() for guard in _GUARDS.values()]
