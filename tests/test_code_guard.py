"""
代码段守卫：论文里的代码清单不该由这个工具来审、来改。

为什么单独做一条机械规则：工具读到的是**抽取出来的文本**，上下文往往不全
（模块级常量、被引用的定义常常根本没写进论文），它没有能力判断代码对不对。
让它"顺手修 bug"，结果就是拿猜测去改正确的代码。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.agents import audit as audit_agent
from app.agents.prompts import AUDIT_HEADER
from app.agents.revision import patches as revision_agent
from app.config import reset_settings
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.text import looks_like_code
from app.utils.tracing import reset_recorders
from tests.fakes import audit_response, fake_factory

CODE_BLOCK = "def _dequeue_and_enqueue(self, keys):\n    batch_size = keys.shape[0]\n    ptr = 0"
ASSIGNMENT_BLOCK = (
    "query_data, query_labels = fs_dataset.load_query_set(FEWSHOT_QUERY_DIR)\n"
    "known_count = len(query_data)"
)

DOC = (
    "# 论文标题\n\n"
    "## 4.4.2 MoCo框架实现\n\n"
    "本模块实现了动量队列的入队与出队逻辑，具体代码如下所示。\n\n"
    f"{CODE_BLOCK}\n\n"
    "以上代码通过环形队列维护负样本，使队列长度保持稳定。\n"
)


class TestLooksLikeCode(unittest.TestCase):
    def test_python_block_is_code(self) -> None:
        self.assertTrue(looks_like_code(CODE_BLOCK))
        self.assertTrue(looks_like_code("for i in range(n):\n    total += i"))
        self.assertTrue(looks_like_code("x = 1\nself.queue.append(x)"))
        self.assertTrue(looks_like_code(ASSIGNMENT_BLOCK))
        self.assertTrue(looks_like_code("ptr = 0"))
        self.assertTrue(looks_like_code("def foo(): return 1"))

    def test_prose_is_not_code(self) -> None:
        self.assertFalse(looks_like_code("这是一句普通的中文正文，没有任何程序语句。"))
        self.assertFalse(looks_like_code("本模块实现了动量队列的入队与出队逻辑。"))
        # 普通正文中的英文术语和中文解释仍可修改。
        self.assertFalse(looks_like_code("本文使用 MoCo 方法实现了对比学习。"))
        # 多行但不是程序语句（表格、列表、公式说明）
        self.assertFalse(looks_like_code("| 方法 | 准确率 |\n| --- | --- |\n| 本文 | 97.14 |"))
        self.assertFalse(looks_like_code("损失函数定义为：\nL = -log(exp(s+)/Σexp(s))"))


class CodeSkippingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-code-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        reset_settings()
        reset_guards()
        reset_recorders()

    def tearDown(self) -> None:
        reset_llm_factory()
        for key in ("PRA_DATA_DIR", "PRA_RUNS_DIR", "PRA_RETRY_MAX_ATTEMPTS"):
            os.environ.pop(key, None)
        reset_settings()
        reset_guards()
        reset_recorders()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


class TestAuditSkipsCode(CodeSkippingTestCase):
    def test_code_issue_downgrades_to_advice(self) -> None:
        """代码问题不再自动改，但**也不许丢**——降级成建议，写进批注给作者看。"""

        set_llm_factory(
            fake_factory(
                {
                    AUDIT_HEADER: audit_response(
                        [
                            {
                                "span_label": "P02",
                                "category": "逻辑结构",
                                "severity": "high",
                                "problem": "ptr 被提前置 0，导致 remaining 计算错误",
                                "evidence": "batch_size = keys.shape[0]",
                                "suggestion": "调整两行顺序",
                            }
                        ]
                    )
                }
            )
        )
        state = {
            "run_id": "code-guard-test",
            "current_document": DOC,
            "document_version": 1,
            "user_request": "全面检查",
        }
        update = audit_agent.run(state, logger=lambda message: None)

        findings = update["audit_findings"]
        self.assertEqual(len(findings), 1, "代码问题被丢掉了")
        self.assertEqual(findings[0]["kind"], "advice")
        self.assertIn("ptr", findings[0]["problem"])
        self.assertEqual(update["audit_meta"]["dropped_code"], 1)


class TestRevisionSkipsCode(CodeSkippingTestCase):
    def test_protected_patch_without_anchor_is_skipped(self) -> None:
        from app.document.patches import Patch

        patch = Patch(
            "patch-lost", "missing", quote="CONST", replace="const", conflict_code="code_protected"
        )
        notes = []
        self.assertEqual(
            revision_agent._drop_code_patches([patch], [], notes, lambda message: None), []
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("未定位", notes[0])

    def test_real_assignment_block_is_not_rewritten_under_r7(self) -> None:
        from app.document.spans import build_spans

        spans = build_spans("# 实现\n\n" + ASSIGNMENT_BLOCK, version=1)
        code_span = next(span for span in spans if "FEWSHOT_QUERY_DIR" in span.text)
        self.assertTrue(looks_like_code(code_span.text))
        patches = revision_agent._unification_patches(
            spans,
            [
                {
                    "id": "R7",
                    "kind": "rule",
                    "status": "active",
                    "text": "全文统一为 fewshot_query_dir",
                }
            ],
        )
        self.assertEqual(patches, [])

    def test_identifier_in_prose_cannot_be_changed(self) -> None:
        from app.document.patches import STATUS_ACCEPTED, Patch, apply_patches, validate_patch
        from app.document.spans import build_spans

        document = "变量 FEWSHOT_QUERY_DIR 用于查询。"
        span = build_spans(document, version=1)[0]
        self.assertFalse(looks_like_code(span.text))
        patch = Patch(
            "patch-001",
            span.id,
            span_label=span.label,
            quote="FEWSHOT_QUERY_DIR",
            replace="fewshot_query_dir",
        )
        self.assertEqual(validate_patch(patch, [span]).conflict_code, "code_protected")
        notes = []
        self.assertEqual(
            revision_agent._drop_code_patches([patch], [span], notes, lambda message: None), []
        )
        self.assertEqual(len(notes), 1)
        # 旧 Checkpoint 里已经接受的补丁也要在真正改文档前再拦一次。
        stale = Patch(
            "patch-002",
            span.id,
            span_label=span.label,
            quote="FEWSHOT_QUERY_DIR",
            replace="fewshot_query_dir",
            status=STATUS_ACCEPTED,
            start_in_span=3,
            end_in_span=20,
        )
        outcome = apply_patches(document, [span], [stale])
        self.assertEqual(outcome.document, document)
        self.assertEqual(stale.conflict_code, "code_protected")
        self.assertEqual(len(outcome.skipped), 1)

    def test_code_patch_is_dropped_and_noted(self) -> None:
        from app.document.patches import Patch
        from app.document.spans import build_spans

        spans = build_spans(DOC, version=1)
        code_span = next(s for s in spans if looks_like_code(s.text))
        prose_span = next(s for s in spans if "本模块实现了" in s.text)

        patches = [
            Patch(
                patch_id="patch-001",
                span_id=code_span.id,
                span_label=code_span.label,
                quote="ptr = 0",
                replace="ptr = 1",
            ),
            Patch(
                patch_id="patch-002",
                span_id=prose_span.id,
                span_label=prose_span.label,
                quote="本模块实现了",
                replace="本模块负责实现",
            ),
        ]
        notes: list[str] = []
        kept = revision_agent._drop_code_patches(patches, spans, notes, lambda message: None)

        self.assertEqual([patch.patch_id for patch in kept], ["patch-002"])
        self.assertEqual(len(notes), 1)
        self.assertIn("代码段", notes[0])
        self.assertIn(code_span.label, notes[0])


if __name__ == "__main__":
    unittest.main()
