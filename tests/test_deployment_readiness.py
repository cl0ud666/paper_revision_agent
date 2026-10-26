"""部署前回归：精确暂停恢复、交互协议、并发容量、路径与最终确认。"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings, reset_settings, validate_run_id
from app.document.docx_redline import applied_rounds
from app.service.runs import RunManager, RunOptions, RunStateError
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import build_checkpointer, reset_checkpointers, setup_checkpointer
from app.workflow.harness import Harness, RunResult
from app.workflow.interaction import auto_response, dispatch_request, prepare_response
from app.workflow.nodes import _merge_unification_items
from tests.fakes import fake_factory
from tests.test_service import DOC, pipeline_responses, wait_for


class DeploymentTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pra-deployment-")
        self.root = Path(self.temp.name)
        self.environment = patch.dict(
            os.environ,
            {
                "PRA_DATA_DIR": str(self.root),
                "PRA_RUNS_DIR": str(self.root / "runs"),
                "PRA_CHECKPOINT_DB": str(self.root / "checkpoints" / "test.sqlite"),
                "PRA_CHECKPOINT_BACKEND": "sqlite",
                "PRA_CHECKPOINT_URL": "",
                "PRA_PATCH_GATE": "0",
                "PRA_FINAL_CONFIRM": "1",
                "PRA_RETRY_MAX_ATTEMPTS": "1",
            },
        )
        self.environment.start()
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        set_llm_factory(fake_factory(pipeline_responses()))

    def tearDown(self):
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        self.environment.stop()
        reset_settings()
        self.temp.cleanup()

    def options(self):
        return RunOptions(user_request="全面检查并修改", document=DOC)


class TestPendingRecovery(DeploymentTestCase):
    def test_final_confirm_survives_restart_and_exclusions_reach_checkpoint(self):
        session = RunManager().create(self.options(), run_id="final-confirm")
        session.start()
        wait_for(session, {"awaiting_human"})
        original_request = session.pending
        self.assertEqual(original_request["type"], "final_confirm")
        # 关闭 SQLite 连接，确保后面的恢复真正重建持久化 Checkpointer。
        reset_checkpointers()
        reset_recorders()
        recovered = RunManager().get(session.run_id)
        self.assertEqual(recovered.pending, original_request)
        payload = json.loads(recovered.session_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pending_request"], original_request)
        recovered.resume({"excluded_ids": ["patch-001"]})
        wait_for(recovered, {"completed", "failed"})
        self.assertEqual(recovered.status, "completed", recovered.error)
        state = recovered._harness.current_state()
        self.assertEqual(state["excluded_patch_ids"], ["patch-001"])
        self.assertEqual(applied_rounds(state), [])
        persisted = json.loads(recovered.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["excluded_patch_ids"], ["patch-001"])
        self.assertEqual(
            json.loads(recovered.session_path.read_text(encoding="utf-8"))["pending_request"], {}
        )
        # 下一轮自动确认仍保留上轮剔除结果，不会重新导出被拒绝的修改。
        harness = recovered._harness
        result = harness.follow_up("继续按原要求检查")
        while result.is_interrupted:
            result = harness.resume(auto_response(result.pending_request))
        self.assertEqual(result.state["excluded_patch_ids"], ["patch-001"])

    def test_legacy_session_recovers_real_checkpoint_not_guessed_patch_gate(self):
        session = RunManager().create(self.options(), run_id="legacy-final")
        session.start()
        wait_for(session, {"awaiting_human"})
        original = session.pending
        payload = json.loads(session.session_path.read_text(encoding="utf-8"))
        payload.pop("pending_request")
        session.session_path.write_text(json.dumps(payload), encoding="utf-8")
        reset_checkpointers()
        recovered = RunManager().get(session.run_id)
        self.assertEqual(recovered.pending, original)
        self.assertEqual(recovered.pending["type"], "final_confirm")

    def test_issue_choices_survive_restart(self):
        session = RunManager().create(
            RunOptions(user_request="全面检查", document="# 正文\n\nMoCo 与 MOCO 均用于实验。"),
            run_id="naming-choice",
        )
        set_llm_factory(fake_factory({"*": {}}))
        session.start()
        wait_for(session, {"awaiting_human", "failed"})
        self.assertEqual(session.status, "awaiting_human", session.error)
        original = session.pending
        self.assertEqual(original["type"], "issue_review")
        reset_checkpointers()
        recovered = RunManager().get(session.run_id)
        self.assertEqual(recovered.pending, original)
        issue = original["issues"][0]
        recovered.resume({"choices": {str(issue["id"]): 0}})
        wait_for(recovered, {"awaiting_human", "completed", "failed"})
        self.assertNotEqual(recovered.status, "failed", recovered.error)
        rules = recovered._harness.current_state()["user_rules"]
        self.assertTrue(any(issue["options"][0] in r["text"] for r in rules))


class TestRunSafety(DeploymentTestCase):
    def test_bad_ids_are_rejected_at_all_entry_points(self):
        for bad in (
            "../outside",
            "..",
            "",
            "C:\\outside",
            "/outside",
            "safe/../outside",
            "a\\b",
            "CON.txt",
            "LPT1",
            "trailing.",
            "evals",
            "x" * 129,
        ):
            with self.subTest(run_id=bad):
                with self.assertRaises(ValueError):
                    validate_run_id(bad)
                with self.assertRaises(ValueError):
                    get_settings().paths.run_dir(bad)
                with self.assertRaises(ValueError):
                    RunManager().create(self.options(), run_id=bad)
                with self.assertRaises(ValueError):
                    Harness().start("检查", DOC, run_id=bad)

    def test_duplicate_run_cannot_overwrite_state(self):
        harness = Harness()
        first = harness.start("检查", DOC, run_id="duplicate")
        self.assertTrue(first.is_interrupted)
        with self.assertRaises(ValueError):
            Harness().start("另一个任务", DOC, run_id="duplicate")
        with self.assertRaises(RunStateError):
            RunManager().create(self.options(), run_id="duplicate")
        self.assertEqual(harness.current_state()["user_request"], "检查")

    def test_precreated_sessions_cannot_bypass_capacity(self):
        manager = RunManager(max_concurrent=1)
        first = manager.create(self.options(), run_id="first")
        second = manager.create(self.options(), run_id="second")
        entered, release = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(5)
            first._apply_result(RunResult(first.run_id, first.thread_id, "completed"))

        with patch.object(first, "_entry_start", blocked):
            first.start()
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(RunStateError):
                    second.start()
                self.assertEqual(second.status, "created")
            finally:
                release.set()
                first._thread.join(5)
        second.start()
        wait_for(second, {"awaiting_human"})
        # 等人时释放名额，恢复时也必须重新获取名额。
        third = manager.create(self.options(), run_id="third")
        with patch.object(third, "_entry_start", lambda: (entered.set(), release.wait(5))):
            release.clear()
            third.start()
            try:
                with self.assertRaises(RunStateError):
                    second.resume({"excluded_ids": []})
                self.assertEqual(second.status, "awaiting_human")
            finally:
                release.set()
                third._thread.join(5)

    def test_old_runner_cannot_release_new_resume_slot(self):
        manager = RunManager(max_concurrent=1)
        session = manager.create(self.options(), run_id="slot-generation")
        another = manager.create(self.options(), run_id="other-slot")
        old_finish, new_finish, new_entered = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def old_run():
            session._apply_result(
                RunResult(
                    session.run_id,
                    session.thread_id,
                    "interrupted",
                    interrupts=[{"type": "final_confirm", "patches": []}],
                )
            )
            old_finish.wait(5)

        def resumed_run():
            new_entered.set()
            new_finish.wait(5)
            session._apply_result(RunResult(session.run_id, session.thread_id, "completed"))

        with (
            patch.object(session, "_entry_start", old_run),
            patch.object(session, "_entry_resume", side_effect=lambda payload: resumed_run()),
        ):
            session.start()
            old_thread = session._thread
            wait_for(session, {"awaiting_human"})
            session.resume({"excluded_ids": []})
            self.assertTrue(new_entered.wait(2))
            try:
                old_finish.set()
                old_thread.join(5)
                with self.assertRaises(RunStateError):
                    another.start()
            finally:
                new_finish.set()
                session._thread.join(5)
        another.start()
        wait_for(another, {"awaiting_human"})

    def test_auto_rules_and_constraints_are_present_before_first_agent(self):
        harness = Harness(auto_approve_constraints=["不要修改实验数值"])
        result = harness.run_to_completion("检查", DOC, rules=["全文统一为「MOCO」"])
        self.assertEqual(result.status, "completed")
        texts = [rule["text"] for rule in result.state["user_rules"]]
        self.assertIn("全文统一为「MOCO」", texts)
        self.assertIn("不要修改实验数值", texts)

    def test_cli_auto_mode_forwards_rule(self):
        import main

        created = []

        def factory(**kwargs):
            harness = Harness(**kwargs)
            created.append(harness)
            return harness

        with patch.object(main, "Harness", side_effect=factory), patch("builtins.print"):
            self.assertEqual(
                main.main(["--text", DOC, "--auto-approve", "--rule", "全文统一为「MOCO」"]), 0
            )
        self.assertIn(
            "全文统一为「MOCO」", [r["text"] for r in created[0].current_state()["user_rules"]]
        )


class TestInteractionProtocol(unittest.TestCase):
    def test_group_exclusion_uses_display_numbers_and_all_members(self):
        patches = [
            {
                "patch_id": "patch-001",
                "span_label": "P01",
                "strategy": "unification",
                "rationale": "全文统一为「MOCO」",
            },
            {
                "patch_id": "patch-002",
                "span_label": "P02",
                "strategy": "unification",
                "rationale": "全文统一为「MOCO」",
            },
            {"patch_id": "patch-003", "strategy": "replacement"},
        ]
        request = {"type": "final_confirm", "patches": _merge_unification_items(patches)}
        self.assertEqual(
            prepare_response(request, {"excluded_ids": [1]})["excluded_ids"],
            ["patch-001", "patch-002"],
        )
        self.assertEqual(
            prepare_response(request, {"excluded_ids": [2]})["excluded_ids"], ["patch-003"]
        )
        with self.assertRaises(ValueError):
            prepare_response(request, {"excluded_ids": [99]})

    def test_structured_choices_and_stale_responses(self):
        request = {
            "type": "issue_review",
            "turn": 2,
            "issues": [{"id": 4, "options": ["全文统一为「MOCO」", "全文统一为「moco」"]}],
        }
        response = prepare_response(request, {"choices": {"4": 1}})
        self.assertIn("全文统一为「moco」", response["rules"][0])
        self.assertEqual(response["turn"], 2)
        with self.assertRaises(ValueError):
            prepare_response(request, {"turn": 1})
        with self.assertRaises(ValueError):
            prepare_response(request, {"choices": {"4": 99}})

    def test_unknown_request_cannot_fall_back_to_wrong_gate(self):
        with self.assertRaises(ValueError):
            dispatch_request({"type": "new_unknown_gate"}, {})


@unittest.skipUnless(
    importlib.util.find_spec("langgraph.checkpoint.postgres"),
    "安装 requirements-web.txt 后验证 PostgreSQL 适配层",
)
class TestPostgresConfiguration(DeploymentTestCase):
    def setUp(self):
        super().setUp()
        # 先导入真实的类，再替换工厂；避免第三方 isinstance 引用到 Mock。
        importlib.import_module("langgraph.checkpoint.postgres")

    def test_postgres_pool_cached_per_process_and_closed(self):
        with (
            patch.dict(
                os.environ,
                {
                    "PRA_CHECKPOINT_BACKEND": "postgres",
                    "PRA_CHECKPOINT_URL": "postgresql://user:secret@localhost/test",
                },
            ),
            patch("psycopg_pool.ConnectionPool") as pool_class,
        ):
            reset_settings()
            saver = build_checkpointer()
            self.assertIs(build_checkpointer(), saver)
            pool_class.return_value.wait.assert_called_once()
            self.assertTrue(pool_class.call_args.kwargs["kwargs"]["autocommit"])
            reset_checkpointers()
            pool_class.return_value.close.assert_called_once()

    def test_postgres_failure_does_not_fall_back_to_memory_or_expose_password(self):
        with (
            patch.dict(
                os.environ,
                {
                    "PRA_CHECKPOINT_BACKEND": "postgres",
                    "PRA_CHECKPOINT_URL": "postgresql://user:secret@localhost/test",
                },
            ),
            patch("psycopg_pool.ConnectionPool") as pool_class,
        ):
            reset_settings()
            pool_class.return_value.wait.side_effect = RuntimeError("secret")
            with self.assertRaises(RuntimeError) as error:
                build_checkpointer()
            self.assertNotIn("secret", str(error.exception))
            pool_class.return_value.close.assert_called_once()

    def test_setup_is_separate_from_regular_task_creation(self):
        with patch("app.workflow.graph.build_checkpointer") as factory:
            setup_checkpointer()
            factory.return_value.setup.assert_called_once()


@unittest.skipUnless(
    os.getenv("PRA_TEST_POSTGRES_URL"), "未配置专用 PostgreSQL 测试库 PRA_TEST_POSTGRES_URL"
)
class TestRealPostgresRecovery(DeploymentTestCase):
    def test_pause_and_resume_after_recreating_pool(self):
        with patch.dict(
            os.environ,
            {
                "PRA_CHECKPOINT_BACKEND": "postgres",
                "PRA_CHECKPOINT_URL": os.environ["PRA_TEST_POSTGRES_URL"],
            },
        ):
            reset_settings()
            setup_checkpointer()
            session = RunManager().create(self.options())
            session.start()
            wait_for(session, {"awaiting_human", "failed"}, timeout=30)
            self.assertEqual(session.status, "awaiting_human", session.error)
            reset_checkpointers()
            recovered = RunManager().get(session.run_id)
            recovered.resume({"excluded_ids": ["patch-001"]})
            wait_for(recovered, {"completed", "failed"}, timeout=30)
            self.assertEqual(recovered.status, "completed", recovered.error)
            self.assertEqual(
                recovered._harness.current_state()["excluded_patch_ids"], ["patch-001"]
            )
