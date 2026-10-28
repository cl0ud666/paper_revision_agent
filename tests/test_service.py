"""
服务层测试：后台执行、人工确认暂停/恢复、状态落盘、跨进程恢复。

全部使用假LLM，不联网。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from app.agents.prompts import (
    AUDIT_HEADER,
    ORCHESTRATOR_HEADER,
    REVIEW_HEADER,
    REVISION_HEADER,
)
from app.config import reset_settings
from app.service.runs import (
    STATUS_AWAITING,
    STATUS_COMPLETED,
    RunManager,
    RunNotFound,
    RunOptions,
    RunStateError,
)
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import reset_checkpointers
from tests.fakes import (
    audit_response,
    fake_factory,
    patch_response,
    plan_response,
    review_response,
)

DOC = """# 测试论文

## 摘要

本文提出了一个新的方法，效果比较好。实验结果表明我们的方法比基线方法好。

## 1 引言

图神经网络在节点分类任务上被广泛使用。
"""

ISSUE_SENTENCE = "本文提出了一个新的方法，效果比较好。"
IMPROVED_SENTENCE = "本文提出了一种面向小样本场景的图神经网络方法，性能优于基线方法。"


def pipeline_responses() -> dict:
    return {
        ORCHESTRATOR_HEADER: plan_response("audit"),
        AUDIT_HEADER: audit_response(
            [
                {
                    "span_label": "P01",
                    "category": "语言表达",
                    "severity": "high",
                    "location": "摘要",
                    "problem": "结论表述过于笼统",
                    "evidence": ISSUE_SENTENCE,
                    "suggestion": "明确方法定位与对比结论",
                }
            ]
        ),
        REVISION_HEADER: patch_response(
            [
                {
                    "span_label": "P01",
                    "issue_id": 1,
                    "quote": ISSUE_SENTENCE,
                    "replace": IMPROVED_SENTENCE,
                    "rationale": "改成具体的对比结论",
                }
            ]
        ),
        REVIEW_HEADER: review_response("pass"),
    }


def wait_for(session, statuses, timeout: float = 10.0) -> str:
    """等到任务进入某个状态（后台线程需要时间）。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        if session.status in statuses:
            return session.status
        time.sleep(0.05)
    raise AssertionError(f"等待状态 {statuses} 超时，当前为 {session.status}（{session.error}）")


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-service-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_CHECKPOINT_DB"] = str(self.tmp_dir / "checkpoints" / "test.sqlite")
        os.environ["PRA_MAX_CONCURRENT_RUNS"] = "2"
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        # 这个文件验证的是"暂停→恢复"，需要补丁闸门存在
        os.environ["PRA_PATCH_GATE"] = "1"
        os.environ["PRA_FINAL_CONFIRM"] = "0"
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        set_llm_factory(fake_factory(pipeline_responses()))

    def tearDown(self) -> None:
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        for key in (
            "PRA_DATA_DIR",
            "PRA_RUNS_DIR",
            "PRA_CHECKPOINT_DB",
            "PRA_MAX_CONCURRENT_RUNS",
            "PRA_RETRY_MAX_ATTEMPTS",
        ):
            os.environ.pop(key, None)
        reset_settings()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def options(self, **overrides) -> RunOptions:
        data = {
            "user_request": "请全面检查并修改这篇论文。",
            "document": DOC,
            "document_name": "sample.md",
            "constraints": ["不要修改实验结果"],
        }
        data.update(overrides)
        return RunOptions(**data)


class TestRunLifecycle(ServiceTestCase):
    def test_start_pause_resume_complete(self) -> None:
        manager = RunManager()
        session = manager.create(self.options(), run_id="paper-test-1")
        self.assertEqual(session.status, "created")

        session.start()
        wait_for(session, {STATUS_AWAITING})

        snapshot = session.snapshot()
        self.assertEqual(snapshot["status"], STATUS_AWAITING)
        self.assertEqual(snapshot["stage"], "awaiting_human")
        # 唯一的闸门：补丁清单（问题清单不再问"有没有要忽略的"）
        self.assertEqual(len(snapshot["patches"]), 1)
        self.assertEqual(snapshot["pending"]["type"], "patch_review")
        self.assertEqual(snapshot["pending"]["patches"][0]["patch_id"], "patch-001")
        self.assertTrue(snapshot["pending"]["patches"][0]["rationale"])
        self.assertGreaterEqual(snapshot["event_cursor"], 2)

        # 只勾选接受第1条补丁
        session.resume({"accepted_ids": ["patch-001"]})
        wait_for(session, {STATUS_COMPLETED})

        snapshot = session.snapshot()
        self.assertEqual(snapshot["review_status"], "pass")
        self.assertEqual(snapshot["iteration"], 1)
        self.assertEqual(len(snapshot["edits"]), 1)
        self.assertEqual(snapshot["patch_stats"]["accepted"], 1)
        self.assertIn(IMPROVED_SENTENCE, session.document_text())
        self.assertIn("学术论文修改报告", session.report_text())
        self.assertTrue(snapshot["has_report"])
        self.assertGreater(snapshot["trace_summary"]["totals"]["calls"], 0)

    def test_auto_approve_runs_to_completion(self) -> None:
        manager = RunManager()
        session = manager.create(
            self.options(auto_approve=True, constraints=[]), run_id="paper-test-auto"
        )
        session.start()
        wait_for(session, {STATUS_COMPLETED})

        snapshot = session.snapshot()
        self.assertEqual(snapshot["status"], STATUS_COMPLETED)
        self.assertEqual(snapshot["pending"], {})
        self.assertIn(IMPROVED_SENTENCE, session.document_text())

    def test_events_are_incremental(self) -> None:
        manager = RunManager()
        session = manager.create(self.options(auto_approve=True), run_id="paper-test-events")
        session.start()
        wait_for(session, {STATUS_COMPLETED})

        first = session.events(0)
        self.assertTrue(first["events"])
        self.assertGreater(first["cursor"], 0)

        # 从最新游标再取：不应重复
        second = session.events(first["cursor"])
        self.assertEqual(second["events"], [])
        self.assertEqual(second["cursor"], first["cursor"])

        # 事件里有Agent维度信息，前端可以直接画时间线
        agents = {event.get("agent") for event in first["events"] if event["type"] == "agent"}
        self.assertIn("audit", agents)
        self.assertIn("revision", agents)
        self.assertIn("review", agents)

    def test_resume_without_pause_is_rejected(self) -> None:
        manager = RunManager()
        session = manager.create(self.options(auto_approve=True), run_id="paper-test-invalid")
        session.start()
        wait_for(session, {STATUS_COMPLETED})
        with self.assertRaises(RunStateError):
            session.resume({"approved_issue_ids": [1]})

    def test_concurrent_limit_and_validation(self) -> None:
        manager = RunManager(max_concurrent=1)
        with self.assertRaises(RunStateError):
            manager.create(self.options(document="   "), run_id="paper-test-empty")
        with self.assertRaises(RunStateError):
            manager.create(self.options(user_request=""), run_id="paper-test-norequest")

        session = manager.create(self.options(), run_id="paper-test-limit")
        session.start()
        # start() 之后状态立刻变成running，此时不允许再提交新任务
        with self.assertRaises(RunStateError):
            manager.create(self.options(), run_id="paper-test-limit-2")
        # 等它停下来等人确认后，模型不再被占用，可以提交新任务
        wait_for(session, {STATUS_AWAITING})
        second = manager.create(self.options(auto_approve=True), run_id="paper-test-limit-3")
        second.start()
        wait_for(second, {STATUS_COMPLETED})


class TestPersistence(ServiceTestCase):
    def test_session_and_state_snapshot_on_disk(self) -> None:
        manager = RunManager()
        session = manager.create(self.options(), run_id="paper-test-files")
        session.start()
        wait_for(session, {STATUS_AWAITING})

        run_dir = self.tmp_dir / "runs" / "paper-test-files"
        for name in ("session.json", "state.json", "trace.jsonl"):
            self.assertTrue((run_dir / name).exists(), name)

        payload = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], STATUS_AWAITING)
        self.assertEqual(payload["thread_id"], "thread-paper-test-files")
        self.assertEqual(payload["options"]["document_name"], "sample.md")

        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("trace", state)
        self.assertEqual(len(state["audit_findings"]), 1)

    def test_recover_after_restart_and_resume(self) -> None:
        """模拟服务重启：新进程里恢复任务并继续（依赖SQLite Checkpoint）。"""

        manager = RunManager()
        session = manager.create(self.options(), run_id="paper-test-restart")
        session.start()
        wait_for(session, {STATUS_AWAITING})
        # 停在唯一的闸门（补丁清单）上，然后模拟重启

        # 丢掉进程内的一切，只留磁盘
        reset_guards()
        reset_recorders()
        manager2 = RunManager()
        recovered = manager2.get("paper-test-restart")

        self.assertEqual(recovered.status, STATUS_AWAITING)
        self.assertEqual(recovered.pending, session.pending)
        # 重启后重建的是“补丁审阅”面板（含每条改动的理由）
        self.assertEqual(recovered.pending["type"], "patch_review")
        self.assertEqual(len(recovered.pending["patches"]), 1)
        self.assertTrue(recovered.pending["patches"][0]["rationale"])
        self.assertIn(ISSUE_SENTENCE, recovered.document_text())

        # 继续执行（Harness用同一个thread_id + 同一个SQLite Checkpoint）
        recovered.resume({"accept_all": True})
        wait_for(recovered, {STATUS_COMPLETED})
        self.assertIn(IMPROVED_SENTENCE, recovered.document_text())

    def test_list_runs_merges_memory_and_disk(self) -> None:
        manager = RunManager()
        session = manager.create(self.options(auto_approve=True), run_id="paper-test-list")
        session.start()
        wait_for(session, {STATUS_COMPLETED})

        # 另开一个manager（模拟重启）后仍能列出历史任务
        runs = RunManager().list_runs()
        ids = [item["run_id"] for item in runs]
        self.assertIn("paper-test-list", ids)
        self.assertTrue(any(item["has_report"] for item in runs))

    def test_unknown_run_raises(self) -> None:
        with self.assertRaises(RunNotFound):
            RunManager().get("does-not-exist")


class TestConcurrentRuns(ServiceTestCase):
    def test_two_runs_do_not_clobber_trace(self) -> None:
        """并发任务各自统计，互不覆盖（回归测试）。"""

        manager = RunManager(max_concurrent=2)
        first = manager.create(self.options(auto_approve=True), run_id="paper-test-a")
        second = manager.create(self.options(auto_approve=True), run_id="paper-test-b")

        first.start()
        second.start()
        wait_for(first, {STATUS_COMPLETED})
        wait_for(second, {STATUS_COMPLETED})

        for session in (first, second):
            summary = session.trace_summary()
            agents = set(summary["per_agent"])
            self.assertIn("audit", agents)
            self.assertIn("revision", agents)
            self.assertGreaterEqual(summary["totals"]["calls"], 3)


if __name__ == "__main__":
    unittest.main()
