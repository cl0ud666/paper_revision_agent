"""
Agent Harness：把Workflow包装成“可运行的一次任务”。

负责：

- 生成run_id / thread_id；
- 用Checkpointer管理暂停（Human-in-the-loop）与恢复（Resume）；
- 统一整理运行结果（报告路径、修改后论文路径、Trace汇总）；
- 支持自动审批模式（评测与命令行无人值守时使用）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from langgraph.types import Command

from app.config import get_settings, validate_run_id
from app.state.schema import initial_state
from app.utils.rules import apply_payload as rules_payload, parse_instruction, rule_texts
from app.utils.tracing import get_recorder, reset_recorders, utc_now
from app.workflow.graph import build_graph
from app.workflow.interaction import auto_response, prepare_response


def new_run_id(prefix: str = "run") -> str:
    """生成run_id：时间戳 + 短随机串（方便按时间排序）。"""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass
class RunResult:
    """一次运行 / 一次恢复之后的结果。"""

    run_id: str
    thread_id: str
    status: str  # completed / interrupted / failed
    state: dict[str, Any] = field(default_factory=dict)
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def is_interrupted(self) -> bool:
        return self.status == "interrupted"

    @property
    def pending_request(self) -> dict[str, Any]:
        """当前等待用户处理的请求（一般是human_approval）。"""

        return self.interrupts[0] if self.interrupts else {}

    @property
    def final_report(self) -> str:
        return self.state.get("final_report", "") or ""

    @property
    def revised_document(self) -> str:
        return self.state.get("current_document", "") or ""

    @property
    def review_status(self) -> str:
        return ((self.state.get("review_result") or {}).get("status") or "") or ""

    @property
    def report_path(self) -> str:
        return self.state.get("report_path", "") or ""

    @property
    def document_path(self) -> str:
        return self.state.get("document_path", "") or ""

    @property
    def docx_path(self) -> str:
        """带Word修订痕迹与批注的副本（上传 .docx时才有）。"""

        return self.state.get("docx_path", "") or ""

    def summary(self) -> dict[str, Any]:
        """给UI / CLI用的精简摘要。"""

        state = self.state
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "plan_kind": state.get("plan_kind", ""),
            "next_action": state.get("next_action", ""),
            "iteration": state.get("iteration", 0),
            "issues": len(state.get("audit_findings") or []),
            "approved": len(state.get("approved_issue_ids") or []),
            "edits": len(state.get("revision_edits") or []),
            "review": self.review_status,
            "report_path": self.report_path,
            "document_path": self.document_path,
            "docx_path": self.docx_path,
        }


class Harness:
    """
    运行入口。

    典型用法::

        harness = Harness()
        result = harness.start(user_request, document)
        while result.is_interrupted:          # 等人（或自动批准）
            payload = ask_user(result.pending_request)
            result = harness.resume(payload)
        print(result.final_report)
    """

    def __init__(
        self,
        *,
        graph: Any | None = None,
        thread_id: str | None = None,
        auto_approve: bool = False,
        auto_approve_constraints: list[str] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = get_settings()
        self.graph = graph or build_graph(settings=self.settings)
        self.thread_id = thread_id or f"thread-{uuid.uuid4().hex[:8]}"
        self.auto_approve = auto_approve
        self.auto_approve_constraints = list(auto_approve_constraints or [])
        self.log = logger or print
        self.run_id = ""

    # ---------- 生命周期 ----------

    def start(
        self,
        user_request: str,
        document: str,
        *,
        run_id: str | None = None,
        max_iterations: int | None = None,
        rules: list[str] | None = None,
        source_docx: str = "",
    ) -> RunResult:
        """
        开始一次新任务。

        :param source_docx: 原始 .docx的路径。给了它，改完就能把改动
            以Word修订痕迹写回一份副本（另存，不动原文件）。
        """

        self.run_id = validate_run_id(run_id if run_id is not None else new_run_id())
        run_dir = self.settings.paths.run_dir(self.run_id)
        if any(
            (run_dir / name).exists()
            for name in ("state.json", "original_document.md", "trace.jsonl", "llm_calls.jsonl")
        ):
            raise ValueError("run_id已有运行产物，请使用新ID或恢复原任务")
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (run_dir / ".started").open("x", encoding="utf-8") as handle:
                handle.write(self.thread_id)
        except FileExistsError:
            raise ValueError("run_id已启动，请使用新ID或恢复原任务") from None
        # 只清掉这个run_id的旧统计；不要动其它正在运行的任务
        reset_recorders(self.run_id)

        state = initial_state(
            user_request,
            document,
            run_id=self.run_id,
            max_iterations=max_iterations or self.settings.workflow.max_iterations,
        )
        state["source_docx"] = source_docx or ""
        # 第一轮也记进turns，这样多轮上下文从第一轮就是完整的
        state["turns"] = [
            {
                "turn": 1,
                "instruction": user_request,
                "created_at": utc_now(),
                "summary": "",
            }
        ]
        state["conversation"] = [{"turn": 1, "role": "user", "content": user_request}]
        rules = list(dict.fromkeys([*self.auto_approve_constraints, *(rules or [])]))
        if rules:
            # 开局就可以给“要求”，例如：全文统一把「土豆」写成「马铃薯」
            seed_rules, _ = rules_payload([], {"rules": list(rules)}, turn=1, source="start")
            state["user_rules"] = seed_rules
            state["human_constraints"] = rule_texts(seed_rules)
        self.log(f"[Harness] 启动任务 {self.run_id}（thread={self.thread_id}）")
        return self._invoke(state)

    def resume(self, payload: dict[str, Any] | None = None) -> RunResult:
        """用用户的输入恢复被中断的Workflow。"""

        request = self.current_pending_request()
        if not request:
            raise RuntimeError("当前任务没有等待人工回答的请求")
        return self._invoke(Command(resume=prepare_response(request, payload or {})))

    def current_pending_request(self) -> dict[str, Any]:
        """从Checkpoint读取真实interrupt，供重启恢复和API使用。"""
        snapshot = self.graph.get_state(self._config())
        pending = list(getattr(snapshot, "interrupts", ()) or ())
        if not pending:
            pending = [
                item
                for task in getattr(snapshot, "tasks", ())
                for item in getattr(task, "interrupts", ())
            ]
        return _extract_interrupts({"__interrupt__": pending})[0] if pending else {}

    def follow_up(
        self,
        instruction: str,
        *,
        constraints: list[str] | None = None,
        rules: list[str] | None = None,
    ) -> RunResult:
        """
        多轮会话：在当前文档（已接受的改动都已生效）基础上继续提新要求。

        例如第一轮“全面检查并修改”，第二轮“把摘要再学术一点”。

        实现要点：

        - 复用同一个thread_id，因此Checkpointer里的历史（已接受的改动、
          被拒绝的改动、审计结果）都还在，模型不会“忘了上下文”；
        - 只重置**本轮**的临时字段（迭代次数、计划、上一轮Review结果）；
        - 被拒绝的改动会作为``rejected_memory``继续带着，避免重复打扰用户。
        """

        state = self.current_state()
        if not state:
            raise RuntimeError("还没有可继续的任务，请先start()")

        previous_turn = int(state.get("turn", 1))
        new_turn = previous_turn + 1

        # 这一轮的话里可能夹着“改要求”的指令（记住/取消/修改某条）
        clean_instruction, rule_payload = parse_instruction(instruction)
        if rules:
            rule_payload.setdefault("rules", []).extend(list(rules))
        if constraints:
            rule_payload.setdefault("rules", []).extend(list(constraints))

        user_rules = list(state.get("user_rules") or [])
        if rule_payload:
            user_rules, added = rules_payload(
                user_rules, rule_payload, turn=new_turn, source="follow_up"
            )
            if added:
                names = "、".join(str(rule.get("id")) for rule in added)
                self.log(f"[Harness] 更新了要求：{names}")

        instruction = clean_instruction or "按最新的要求继续检查并修改。"

        conversation = list(state.get("conversation") or [])
        conversation.append({"turn": new_turn, "role": "user", "content": instruction})

        # 记录上一轮的结果摘要（多轮上下文）
        turns = list(state.get("turns") or [])
        if turns:
            turns[-1] = {**turns[-1], "summary": self._turn_summary(state)}
        turns.append(
            {
                "turn": new_turn,
                "instruction": instruction,
                "created_at": utc_now(),
                "summary": "",
            }
        )

        update: dict[str, Any] = {
            "user_request": instruction,
            "turn": new_turn,
            "turn_started_at": utc_now(),
            "iteration": 0,
            "next_action": "",
            "plan": [],
            "plan_reason": "",
            "next_instructions": "",
            "review_result": None,
            "proposed_patches": [],
            "patch_decisions": {},
            "patch_stats": {},
            "conflict_patches": [],
            "revision_summary": "",
            "conversation": conversation,
            "turns": turns,
            "user_rules": user_rules,
            "human_constraints": rule_texts(user_rules),
        }
        if constraints is not None:
            update["human_constraints"] = list(constraints)

        self.log(f"[Harness] 进入第 {new_turn} 轮会话：{instruction[:40]}")
        return self._invoke(update)

    def current_state(self) -> dict[str, Any]:
        """从Checkpointer读取当前任务状态（多轮 / 恢复时用）。"""

        try:
            snapshot = self.graph.get_state(self._config())
        except Exception:  # noqa: BLE001
            return {}
        values = getattr(snapshot, "values", None)
        return dict(values) if isinstance(values, dict) else {}

    def _turn_summary(self, state: dict[str, Any]) -> str:
        """一句话总结上一轮做了什么（写进多轮上下文）。"""

        applied = len(state.get("applied_patches") or [])
        rejected = len(state.get("rejected_patches") or [])
        review = (state.get("review_result") or {}).get("status", "")
        parts = [f"应用 {applied} 条改动", f"拒绝 {rejected} 条"]
        if review:
            parts.append(f"Review={str(review).upper()}")
        return "，".join(parts)

    def _config(self) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": self.settings.workflow.recursion_limit,
        }

    def run_to_completion(
        self,
        user_request: str,
        document: str,
        *,
        run_id: str | None = None,
        max_iterations: int | None = None,
        source_docx: str = "",
        rules: list[str] | None = None,
    ) -> RunResult:
        """
        一路跑到结束（遇到人工节点时自动批准）。

        用于命令行 --auto-approve与批量评测。
        """

        result = self.start(
            user_request,
            document,
            run_id=run_id,
            max_iterations=max_iterations,
            source_docx=source_docx,
            rules=rules,
        )
        guard = 0
        while result.is_interrupted and guard < 10:
            guard += 1
            payload = self._auto_payload(result.pending_request)
            result = self.resume(payload)
        return result

    # ---------- 内部 ----------

    def _invoke(self, payload: Any) -> RunResult:
        config = self._config()
        run_id = self.run_id or "adhoc"

        try:
            raw = self.graph.invoke(payload, config=config)
        except Exception as exc:  # noqa: BLE001 - 运行级异常也要有明确结果
            self.log(f"[Harness] 运行失败：{type(exc).__name__}: {exc}")
            return RunResult(
                run_id=run_id,
                thread_id=self.thread_id,
                status="failed",
                state={},
                error=f"{type(exc).__name__}: {exc}",
            )

        state = {key: value for key, value in raw.items() if key != "__interrupt__"}
        interrupts = _extract_interrupts(raw)

        if interrupts:
            self.log("[Harness] Workflow暂停，等待人工确认。")
            return RunResult(
                run_id=run_id,
                thread_id=self.thread_id,
                status="interrupted",
                state=state,
                interrupts=interrupts,
            )

        review_status = (state.get("review_result") or {}).get("status", "未执行")
        self.log(f"[Harness] Workflow结束（Review={review_status}）。")
        return RunResult(
            run_id=run_id,
            thread_id=self.thread_id,
            status="completed",
            state=state,
        )

    def _auto_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        """自动审批：全接受 / 全批准，并带上预设约束。"""

        return auto_response(request, self.auto_approve_constraints)

    def trace_summary(self) -> dict[str, Any]:
        """当前run的Trace汇总。"""

        return get_recorder(self.run_id or "adhoc").summary()


def _extract_interrupts(raw: Any) -> list[dict[str, Any]]:
    """把LangGraph的 __interrupt__ 结果规范成dict列表。"""

    if not isinstance(raw, dict):
        return []
    payload = raw.get("__interrupt__")
    if not payload:
        return []

    items = payload if isinstance(payload, (list, tuple)) else [payload]
    results: list[dict[str, Any]] = []
    for item in items:
        value = getattr(item, "value", item)
        results.append(value if isinstance(value, dict) else {"raw": value})
    return results
