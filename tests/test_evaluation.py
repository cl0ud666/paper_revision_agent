"""
评测闭环测试：Golden Set 运行、LLM-as-Judge 解析、回归退化检测。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents.prompts import (
    AUDIT_HEADER,
    ORCHESTRATOR_HEADER,
    REVIEW_HEADER,
    REVISION_HEADER,
)
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from evals import regression, runner
from evals.judges import JUDGE_HEADER, DIMENSIONS, judge_pair
from tests.fakes import (
    audit_response,
    fake_factory,
    patch_response,
    plan_response,
    review_response,
)

TEXT = "本文提出了一种新的方法，该方法可以有效地提高了准确率。"
FIXED = "本文提出了一种新的方法，该方法可以有效提高准确率。"


def fake_judge(original: str, revised: str, **kwargs) -> dict:
    """固定分数的假 Judge，避免测试依赖网络。"""

    return {
        "scores": {"语法": 5, "原意": 4, "学术规范": 4, "降重": 5},
        "average": 4.5,
        "preserved_meaning": True,
        "changed_facts": [],
        "reason": "测试用评分",
        "error": "",
    }


class EvaluationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-eval-"))
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        reset_settings()
        reset_guards()
        reset_recorders()

        self.golden = self.tmp_dir / "golden.jsonl"
        self.golden.write_text(
            "\n".join(
                json.dumps(
                    {
                        "id": f"g{i:03d}",
                        "dimension": "语法",
                        "text": TEXT,
                        "problem": "语法错误",
                        "expectation": "修正语病",
                        "must_keep": ["准确率"],
                    },
                    ensure_ascii=False,
                )
                for i in range(1, 4)
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        reset_llm_factory()
        reset_guards()
        reset_recorders()
        for key in ("PRA_RUNS_DIR", "PRA_DATA_DIR", "PRA_RETRY_MAX_ATTEMPTS"):
            os.environ.pop(key, None)
        reset_settings()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def install_pipeline(self, review="pass", **review_extra) -> None:
        set_llm_factory(
            fake_factory(
                {
                    ORCHESTRATOR_HEADER: plan_response("audit"),
                    AUDIT_HEADER: audit_response(
                        [
                            {
                                "span_label": "P01",
                                "category": "语言表达",
                                "severity": "high",
                                "problem": "存在语法与搭配错误",
                                "evidence": TEXT,
                                "suggestion": "修正能愿动词后的搭配",
                            }
                        ]
                    ),
                    REVISION_HEADER: patch_response(
                        [
                            {
                                "span_label": "P01",
                                "issue_id": 1,
                                "quote": TEXT,
                                "replace": FIXED,
                                "rationale": "修正搭配错误",
                            }
                        ]
                    ),
                }
            )
        )


class TestGoldenSetLoading(EvaluationTestCase):
    def test_load_golden_set(self) -> None:
        items = runner.load_golden_set(self.golden)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["id"], "g001")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            runner.load_golden_set(self.tmp_dir / "不存在.jsonl")

    def test_aggregate(self) -> None:
        records = [
            {
                "scores": {"语法": 5, "原意": 4, "学术规范": 3, "降重": 2},
                "average": 3.5,
                "review_status": "pass",
                "changed": True,
                "preserved_meaning": True,
            },
            {
                "scores": {"语法": 3, "原意": 2, "学术规范": 1, "降重": 4},
                "average": 2.5,
                "review_status": "fail",
                "changed": False,
                "preserved_meaning": False,
            },
        ]
        aggregate = runner.aggregate(records)
        self.assertEqual(aggregate["items"], 2)
        self.assertEqual(aggregate["dimensions"]["语法"], 4.0)
        self.assertEqual(aggregate["overall"], 3.0)
        self.assertEqual(aggregate["review_pass_rate"], 0.5)
        self.assertEqual(aggregate["meaning_preserved_rate"], 0.5)


class TestEvaluateRun(EvaluationTestCase):
    def test_evaluate_writes_results(self) -> None:
        # Review 也用假模型返回 pass
        from app.agents.prompts import REVIEW_HEADER
        from tests.fakes import review_response

        set_llm_factory(
            fake_factory(
                {
                    ORCHESTRATOR_HEADER: plan_response("audit"),
                    AUDIT_HEADER: audit_response(
                        [
                            {
                                "span_label": "P01",
                                "category": "语言表达",
                                "severity": "high",
                                "problem": "存在语法与搭配错误",
                                "evidence": TEXT,
                                "suggestion": "修正能愿动词后的搭配",
                            }
                        ]
                    ),
                    REVISION_HEADER: patch_response(
                        [
                            {
                                "span_label": "P01",
                                "issue_id": 1,
                                "quote": TEXT,
                                "replace": FIXED,
                                "rationale": "修正搭配错误",
                            }
                        ]
                    ),
                    REVIEW_HEADER: review_response("pass"),
                }
            )
        )

        output = self.tmp_dir / "eval.json"
        result = runner.evaluate(
            limit=2,
            tag="test",
            output=output,
            golden_path=self.golden,
            judge=fake_judge,
            log=lambda message: None,
        )

        self.assertTrue(output.exists())
        self.assertEqual(result["aggregate"]["items"], 2)
        self.assertEqual(result["aggregate"]["overall"], 4.5)
        self.assertEqual(result["aggregate"]["changed_rate"], 1.0)
        self.assertEqual(result["items"][0]["revised"], FIXED)
        self.assertIn("fingerprint", result)


class TestJudgeParsing(EvaluationTestCase):
    def test_judge_pair_parses_scores(self) -> None:
        set_llm_factory(
            fake_factory(
                {
                    JUDGE_HEADER: json.dumps(
                        {
                            "语法": 5,
                            "原意": 7,
                            "学术规范": 4,
                            "降重": 3,
                            "preserved_meaning": True,
                            "changed_facts": [],
                            "reason": "测试",
                        },
                        ensure_ascii=False,
                    )
                }
            )
        )
        result = judge_pair(TEXT, FIXED, focus="语法")
        self.assertEqual(result["scores"]["语法"], 5)
        self.assertEqual(result["scores"]["原意"], 5)  # 超出范围被截断到 5
        self.assertEqual(result["average"], 4.25)
        self.assertEqual(result["error"], "")

    def test_judge_pair_handles_bad_output(self) -> None:
        set_llm_factory(fake_factory({JUDGE_HEADER: "完全不是 JSON"}))
        result = judge_pair(TEXT, FIXED)
        self.assertEqual(result["average"], 0.0)
        self.assertTrue(result["error"] or result["average"] == 0)


class TestRegression(EvaluationTestCase):
    def _result(self, tag: str, scores: dict[str, float], pass_rate: float = 0.8) -> dict:
        return {
            "tag": tag,
            "fingerprint": {"model": "deepseek-chat"},
            "aggregate": {
                "dimensions": scores,
                "overall": round(sum(scores.values()) / len(scores), 3),
                "review_pass_rate": pass_rate,
            },
        }

    def test_no_degradation(self) -> None:
        base = self._result("v1", {d: 4.0 for d in DIMENSIONS})
        cand = self._result("v2", {d: 4.1 for d in DIMENSIONS})
        comparison = regression.compare(base, cand, threshold=0.2)
        self.assertFalse(comparison["degraded"])
        self.assertTrue(comparison["fingerprint_match"])

    def test_degradation_detected(self) -> None:
        base = self._result("v1", {d: 4.0 for d in DIMENSIONS})
        candidate_scores = {d: 4.0 for d in DIMENSIONS}
        candidate_scores["原意"] = 3.5
        cand = self._result("v2", candidate_scores)
        comparison = regression.compare(base, cand, threshold=0.2)
        self.assertTrue(comparison["degraded"])
        self.assertTrue(any("原意" in reason for reason in comparison["reasons"]))

    def test_main_exit_code_blocks_iteration(self) -> None:
        base_path = self.tmp_dir / "base.json"
        cand_path = self.tmp_dir / "cand.json"
        base_path.write_text(
            json.dumps(self._result("v1", {d: 4.5 for d in DIMENSIONS}), ensure_ascii=False),
            encoding="utf-8",
        )
        cand_path.write_text(
            json.dumps(self._result("v2", {d: 3.5 for d in DIMENSIONS}), ensure_ascii=False),
            encoding="utf-8",
        )
        code = regression.main([str(base_path), str(cand_path), "--threshold", "0.2"])
        self.assertEqual(code, 1)

    def test_format_report_contains_table(self) -> None:
        base = self._result("v1", {d: 4.0 for d in DIMENSIONS})
        cand = self._result("v2", {d: 4.0 for d in DIMENSIONS})
        text = regression.format_report(regression.compare(base, cand))
        self.assertIn("回归比较", text)
        self.assertIn("语法", text)


if __name__ == "__main__":
    unittest.main()
