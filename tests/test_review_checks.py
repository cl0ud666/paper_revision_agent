"""
Review确定性检查测试。

即使模型说pass，这些硬检查也必须把明显问题压回fail：

1. 内容大幅变短（内容丢失）
2. 整段重复（替换成加长版导致的复制粘贴）
3. 目标问题一处修改都没落实
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents import review as review_agent
from app.agents.prompts import REVIEW_HEADER
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.text import find_duplicate_paragraphs
from app.utils.tracing import reset_recorders
from tests.fakes import fake_factory, review_response

PARAGRAPH = (
    "本文提出的方法由三个部分组成，分别是特征编码模块、结构聚合模块以及原型分类模块，"
    "其中特征编码模块负责把原始特征映射到新的空间，结构聚合模块负责利用图结构信息，"
    "原型分类模块负责根据类原型完成分类，三个模块联合训练以获得更好的性能。"
)


def base_state(current_document: str) -> dict:
    return {
        "run_id": "review-check",
        "user_request": "全面检查并修改这篇论文。",
        "original_document": PARAGRAPH + "\n\n第二段内容。\n",
        "current_document": current_document,
        "audit_findings": [
            {
                "id": 1,
                "category": "方法描述",
                "severity": "high",
                "problem": "方法描述不完整",
                "evidence": PARAGRAPH,
                "suggestion": "补充细节",
            }
        ],
        "approved_issue_ids": [1],
        "human_constraints": [],
        "iteration": 1,
        "max_iterations": 3,
        "revision_summary": "补充了方法细节",
        "revision_edits": [
            {
                "issue_id": 1,
                "original": PARAGRAPH,
                "revised": PARAGRAPH + "并补充了实现细节。",
            }
        ],
        "revision_failed_edits": [],
    }


class TestReviewChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-review-"))
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        reset_settings()
        reset_guards()
        reset_recorders()

    def tearDown(self) -> None:
        reset_llm_factory()
        os.environ.pop("PRA_RUNS_DIR", None)
        reset_settings()
        reset_guards()
        reset_recorders()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_find_duplicate_paragraphs(self) -> None:
        text = f"{PARAGRAPH}\n\n中间段落。\n\n{PARAGRAPH}\n\n短段。\n\n短段。"
        duplicates = find_duplicate_paragraphs(text)
        self.assertEqual(len(duplicates), 1)

    def test_duplicate_paragraph_forces_fail(self) -> None:
        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        duplicated = f"{PARAGRAPH}\n\n第二段内容。\n\n{PARAGRAPH}\n"
        update = review_agent.run(base_state(duplicated), logger=lambda message: None)

        self.assertEqual(update["review_result"]["status"], "fail")
        problems = " ".join(
            issue.get("problem", "") for issue in update["review_result"]["remaining_issues"]
        )
        self.assertIn("重复", problems)

    def test_short_document_forces_fail(self) -> None:
        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        update = review_agent.run(base_state("只剩一句话。"), logger=lambda message: None)

        self.assertEqual(update["review_result"]["status"], "fail")
        checks = {check["name"]: check["status"] for check in update["review_result"]["checks"]}
        self.assertEqual(checks["内容完整度"], "fail")

    def test_no_applied_edits_forces_fail(self) -> None:
        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        state = base_state(PARAGRAPH + "\n\n第二段内容。\n")
        state["revision_edits"] = []
        update = review_agent.run(state, logger=lambda message: None)

        self.assertEqual(update["review_result"]["status"], "fail")
        checks = {check["name"]: check["status"] for check in update["review_result"]["checks"]}
        self.assertEqual(checks["修改是否落地"], "fail")

    def test_clean_case_can_pass(self) -> None:
        set_llm_factory(fake_factory({REVIEW_HEADER: review_response("pass")}))
        improved = (
            PARAGRAPH.replace("负责利用图结构信息", "负责利用图结构信息并给出实现细节")
            + "\n\n第二段内容。\n"
        )
        update = review_agent.run(base_state(improved), logger=lambda message: None)

        self.assertEqual(update["review_result"]["status"], "pass")
        checks = {check["name"]: check["status"] for check in update["review_result"]["checks"]}
        self.assertEqual(checks["段落重复"], "ok")
        self.assertEqual(checks["内容完整度"], "ok")

    def test_review_context_does_not_repeat_document(self) -> None:
        """复核上下文里不应当出现两份完整文档。"""

        from app.utils.context import build_context

        state = base_state(PARAGRAPH + "\n\n第二段内容。\n")
        fields = build_context(
            state, "review", extra={"revised_excerpt": state["current_document"]}
        )
        self.assertNotIn("chunk_text", fields)
        self.assertIn("revised_excerpt", fields)


if __name__ == "__main__":
    unittest.main()
