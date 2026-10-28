"""
“用户要求与决定”存储测试：增删改、忽略指纹、指令解析、模型调用日志。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import reset_settings
from app.utils import rules as rules_module
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import AgentGuard, CircuitBreaker, RetryPolicy, reset_guards
from app.utils.tracing import reset_recorders
from tests.fakes import FakeChatModel, fake_factory


class TestRuleStore(unittest.TestCase):
    def test_add_and_describe(self) -> None:
        rules, rule = rules_module.add_rule([], "全文统一把「土豆」写成「马铃薯」", turn=1)
        self.assertEqual(rule["id"], "R1")
        self.assertEqual(rule["kind"], "rule")
        self.assertEqual(rule["status"], "active")

        rules, second = rules_module.add_rule(rules, "不要改实验数据", turn=1)
        self.assertEqual(second["id"], "R2")

        text = rules_module.describe(rules, kinds=("rule",))
        self.assertIn("[R1]", text)
        self.assertIn("马铃薯", text)
        self.assertIn("[R2]", text)

    def test_rule_texts_only_active(self) -> None:
        rules, _ = rules_module.add_rule([], "要求一")
        rules, _ = rules_module.add_rule(rules, "要求二")
        rules = rules_module.revoke_rule(rules, "R1", turn=2)

        self.assertEqual(rules_module.rule_texts(rules), ["要求二"])
        self.assertEqual(rules[0]["status"], "revoked")
        self.assertEqual(rules[0]["updated_turn"], 2)

    def test_update_rule(self) -> None:
        rules, _ = rules_module.add_rule([], "旧的写法")
        rules = rules_module.update_rule(rules, "R1", "新的写法", turn=3)
        self.assertEqual(rules_module.rule_texts(rules), ["新的写法"])
        self.assertEqual(rules[0]["updated_turn"], 3)

    def test_revoked_rule_can_be_reactivated_by_update(self) -> None:
        rules, _ = rules_module.add_rule([], "要求")
        rules = rules_module.revoke_rule(rules, "R1")
        rules = rules_module.update_rule(rules, "R1", "改回来")
        self.assertEqual(rules_module.rule_texts(rules), ["改回来"])

    def test_ignore_issues_records_signature(self) -> None:
        issues = [
            {
                "id": 2,
                "span_label": "P05",
                "category": "学术规范",
                "problem": "参考文献格式不统一",
                "evidence": "[1] Kipf T N",
            }
        ]
        rules, created = rules_module.ignore_issues([], issues, turn=1)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["kind"], "ignore")
        self.assertIn("参考文献", created[0]["text"])
        self.assertEqual(created[0]["signature"]["span_label"], "P05")

        # 下一轮重新审查：问题编号变了，但同一个问题仍应被认出
        again = {
            "id": 9,
            "span_label": "P05",
            "category": "学术规范",
            "problem": "参考文献格式不统一，缺少出版信息",
            "evidence": "[1] Kipf T N",
        }
        self.assertTrue(rules_module.is_ignored(again, rules))

        different = {
            "id": 10,
            "span_label": "P12",
            "category": "实验分析",
            "problem": "结果分析不足",
        }
        self.assertFalse(rules_module.is_ignored(different, rules))

    def test_revoking_ignore_rule_restores_scope(self) -> None:
        issues = [{"id": 1, "span_label": "P05", "problem": "格式不统一"}]
        rules, _ = rules_module.ignore_issues([], issues)
        rules = rules_module.revoke_rule(rules, "R1")
        self.assertFalse(rules_module.is_ignored(issues[0], rules))

    def test_apply_payload_variants(self) -> None:
        issues = [{"id": 1, "problem": "问题一"}, {"id": 2, "problem": "问题二"}]

        rules, created = rules_module.apply_payload(
            [], {"rules": ["要求甲；要求乙"]}, issues=issues
        )
        self.assertEqual(len(created), 2)
        self.assertTrue(issues[0].get("ignored") is None)

        rules2, created2 = rules_module.apply_payload(rules, {"ignore_ids": [2]}, issues=issues)
        self.assertEqual(len(created2), 1)
        self.assertTrue(issues[1]["ignored"])

        rules3, _ = rules_module.apply_payload(rules2, {"ignore_all": True}, issues=issues)
        self.assertTrue(all(issue["ignored"] for issue in issues))
        self.assertEqual(len(rules_module.active_rules(rules3)), 4)

    def test_parse_instruction(self) -> None:
        clean, payload = rules_module.parse_instruction(
            "记住：全文统一把土豆写成马铃薯\n把引言写具体一点"
        )
        self.assertEqual(payload["rules"], ["全文统一把土豆写成马铃薯"])
        self.assertEqual(clean, "把引言写具体一点")

        _, payload = rules_module.parse_instruction("取消 R1")
        self.assertEqual(payload["revoke"], ["R1"])

        _, payload = rules_module.parse_instruction("R2 改成 全文用「马铃薯」")
        self.assertEqual(payload["update"], {"R2": "全文用「马铃薯」"})

        clean, payload = rules_module.parse_instruction("把摘要改短一点")
        self.assertEqual(payload, {})
        self.assertEqual(clean, "把摘要改短一点")

    def test_parse_instruction_inline(self) -> None:
        """命令行里换行不方便，一行用「；」分隔也要能识别。"""

        clean, payload = rules_module.parse_instruction("取消 R1；把引言写具体")
        self.assertEqual(payload["revoke"], ["R1"])
        self.assertEqual(clean, "把引言写具体")


class TestCallObservability(unittest.TestCase):
    """① 每次调用都要有输出：控制台一行摘要 + 落盘完整Prompt/返回。"""

    def setUp(self) -> None:
        self._original_pra_env = {
            key: os.environ.get(key) for key in ("PRA_DATA_DIR", "PRA_RUNS_DIR")
        }
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-rules-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        reset_settings()
        reset_guards()
        reset_recorders()

    def tearDown(self) -> None:
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        for key, value in self._original_pra_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_call_is_logged_to_file(self) -> None:
        from app.utils.llm import call_llm

        set_llm_factory(fake_factory({"*": '{"ok": true}'}))
        response = call_llm(
            "请输出 JSON",
            agent="audit",
            run_id="log-test",
            action="chunk_1",
        )

        self.assertTrue(response.text)
        path = self.tmp_dir / "runs" / "log-test" / "llm_calls.jsonl"
        self.assertTrue(path.exists())

        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["agent"], "audit")
        self.assertEqual(record["action"], "chunk_1")
        self.assertIn("请输出 JSON", record["prompt"])
        self.assertEqual(record["response"], '{"ok": true}')
        self.assertGreater(record["total_tokens"], 0)

    def test_guard_prints_agent_activity(self) -> None:
        """调用Agent时控制台要有可读的进展输出。"""

        messages: list[str] = []
        guard = AgentGuard(
            name="orchestrator",
            retry=RetryPolicy(max_attempts=1),
            breaker=CircuitBreaker("orchestrator", failure_threshold=5),
            default_timeout=5.0,
            on_event=messages.append,
        )

        guard.run(lambda: FakeChatModel({"*": '{"next_action": "audit"}'}).invoke([("human", "x")]))

        self.assertTrue(messages)
        self.assertIn("orchestrator", messages[-1])
        self.assertIn("完成", messages[-1])
        self.assertIn("next_action", messages[-1])


if __name__ == "__main__":
    unittest.main()
