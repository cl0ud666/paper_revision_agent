"""
Audit并发审查 + 跨批对齐 + 全文矛盾核查测试。

覆盖：

1. 分块确实**并发**执行（同时有几个调用在飞）；
2. ``PRA_AUDIT_CONCURRENCY=1``时退回串行；
3. 结果按**分块序号**归并：完成顺序被打乱也不影响结论；
4. 跨批对齐：模型指出的重复候选按编号被合并（且只在编号合法时生效）；
5. 跨批对齐：只有全局视角才发现的问题被补充进来；
6. 全文矛盾核查：整篇送审，结果并入findings；
7. 新增环节失败时优雅降级，不影响原有分块结论。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from app.agents import audit as audit_agent
from app.agents.prompts import (
    AUDIT_HEADER,
    CROSS_CHECK_HEADER,
    TARGETED_CHECK_HEADER,
    WHOLE_PAPER_HEADER,
)
from app.config import get_settings, reset_settings
from app.document.spans import build_spans
from app.utils.llm import reset_llm_factory, set_llm_factory
from app.utils.resilience import reset_guards
from app.utils.tracing import reset_recorders
from app.workflow.graph import reset_checkpointers
from tests.fakes import (
    FakeChatModel,
    audit_response,
    cross_check_response,
    fake_factory,
    targeted_response,
    whole_paper_response,
)

# 六个正文段落：用PRA_AUDIT_CHUNK_TOKENS=200会切成多块
PARAGRAPHS = [
    "近年来，图神经网络在节点分类任务上被广泛使用，已经成为该领域最重要的方法之一，吸引了大量研究者的关注。",
    "但是实际场景中的图数据往往存在标签稀疏的问题，标注成本很高，这严重限制了模型在真实任务上的表现。",
    "本文提出了一种面向小样本场景的图神经网络方法，并在三个公开数据集上进行了充分的对比实验与验证。",
    "实验结果显示，我们提出的方法在各项指标上都优于现有的基线方法，提升幅度明显，验证了方法的有效性。",
    "消融实验进一步说明，本文设计的注意力聚合模块对最终性能有着重要贡献，去掉它以后指标会明显下降。",
    "综上所述，本文的方法在多个评价指标上都优于现有工作，具有良好的推广价值和实际应用前景，值得进一步研究。",
]

DOC = (
    "# 面向小样本场景的图神经网络节点分类方法研究\n\n"
    "## 摘要\n\n"
    "本文研究小样本场景下的节点分类问题，提出了一种新的聚合方法，并在多个数据集上验证了效果。\n\n"
    "## 1 引言\n\n" + "\n\n".join(PARAGRAPHS) + "\n"
)


def _label_text() -> dict[str, str]:
    """段落标签 -> 段落原文（用来构造逐字可核对的evidence）。"""

    return {span.label: span.text for span in build_spans(DOC, version=1)}


def _chunk_number(prompt: str) -> int:
    match = re.search(r"第\s*(\d+)\s*/\s*\d+\s*块", prompt)
    return int(match.group(1)) if match else 1


def _first_label(prompt: str) -> str:
    match = re.search(r"包含段落\s*(P\d+)", prompt)
    if match:
        return match.group(1)
    match = re.search(r"\[(P\d+)\]", prompt)
    return match.group(1) if match else "P01"


class _Tracker:
    """记录同时在飞的LLM调用数（用来证明分块确实在并发）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0

    def enter(self) -> None:
        with self.lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self) -> None:
        with self.lock:
            self.inflight -= 1


class _TrackingModel(FakeChatModel):
    def __init__(self, responses: dict[str, Any], tracker: _Tracker) -> None:
        super().__init__(responses)
        self._tracker = tracker

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        self._tracker.enter()
        try:
            return super().invoke(messages, **kwargs)
        finally:
            self._tracker.leave()


class AuditConcurrencyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="pra-audit-conc-"))
        os.environ["PRA_DATA_DIR"] = str(self.tmp_dir)
        os.environ["PRA_RUNS_DIR"] = str(self.tmp_dir / "runs")
        os.environ["PRA_RETRY_MAX_ATTEMPTS"] = "1"
        os.environ["PRA_AUDIT_CHUNK_TOKENS"] = "200"
        os.environ["PRA_AUDIT_MIN_SEVERITY"] = "low"
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()

    def tearDown(self) -> None:
        reset_llm_factory()
        for key in (
            "PRA_DATA_DIR",
            "PRA_RUNS_DIR",
            "PRA_RETRY_MAX_ATTEMPTS",
            "PRA_AUDIT_CHUNK_TOKENS",
            "PRA_AUDIT_MIN_SEVERITY",
            "PRA_AUDIT_CONCURRENCY",
            "PRA_AUDIT_CROSS_CHECK",
            "PRA_AUDIT_TARGETED",
            "PRA_AUDIT_TARGETED_TOKENS",
            "PRA_AUDIT_WHOLE_PAPER",
            "PRA_AUDIT_WHOLE_PAPER_TOKENS",
            "PRA_AUDIT_WHOLE_PAPER_SMALL_DOC",
            "PRA_AUDIT_CROSS_CHECK_TOKENS",
        ):
            os.environ.pop(key, None)
        reset_settings()
        reset_guards()
        reset_recorders()
        reset_checkpointers()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # ---------- 辅助 ----------

    def env(self, **pairs: Any) -> None:
        for key, value in pairs.items():
            os.environ[key] = str(value)
        reset_settings()

    def run_audit(
        self,
        responses: dict[str, Any],
        *,
        tracker: _Tracker | None = None,
        calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tracker = tracker or _Tracker()

        def factory(agent: str, overrides: dict[str, Any]) -> _TrackingModel:
            model = _TrackingModel(responses, tracker)
            if calls is not None:
                model.calls = calls
            return model

        set_llm_factory(factory)
        state = {
            "run_id": "audit-concurrency-test",
            "current_document": DOC,
            "document_version": 1,
            "user_request": "全面检查",
        }
        return audit_agent.run(state, logger=lambda message: None)

    def chunk_reporting(self, problems: dict[int, str], *, delays: dict[int, float] | None = None):
        """
        构造一个"每个分块报一条问题"的假模型响应。

        problems: 分块序号（从1开始）-> 问题描述。
        delays: 分块序号 -> 该分块故意拖延的秒数（用来打乱完成顺序）。
        """

        texts = _label_text()
        delays = delays or {}

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            time.sleep(delays.get(index, 0.0))
            problem = problems.get(index)
            if problem is None:
                return audit_response([])
            label = _first_label(prompt)
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                        "suggestion": "改得更清楚",
                    }
                ]
            )

        return respond


class TestChunkConcurrency(AuditConcurrencyTestCase):
    def test_chunks_run_in_parallel(self) -> None:
        """三个分块应当同时在飞，而不是一个一个跑。"""

        self.env(PRA_AUDIT_CONCURRENCY=3, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        tracker = _Tracker()
        update = self.run_audit(
            {
                AUDIT_HEADER: self.chunk_reporting(
                    {
                        1: "句子成分残缺，缺少主语",
                        2: "论证跳跃，缺少必要的过渡",
                        3: "结论表述过于绝对",
                    },
                    delays={1: 0.05, 2: 0.05, 3: 0.05},
                )
            },
            tracker=tracker,
        )

        self.assertGreater(update["audit_meta"]["chunks"], 1)
        self.assertGreater(update["audit_meta"]["chunk_concurrency"], 1)
        self.assertGreater(tracker.max_inflight, 1, "分块没有并发执行")

    def test_concurrency_one_falls_back_to_serial(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        tracker = _Tracker()
        update = self.run_audit(
            {
                AUDIT_HEADER: self.chunk_reporting(
                    {
                        1: "句子成分残缺，缺少主语",
                        2: "论证跳跃，缺少必要的过渡",
                        3: "结论表述过于绝对",
                    },
                    delays={1: 0.02, 2: 0.02, 3: 0.02},
                )
            },
            tracker=tracker,
        )

        self.assertEqual(update["audit_meta"]["chunk_concurrency"], 1)
        self.assertEqual(tracker.max_inflight, 1, "并发度设为 1 时不应该并发")

    def test_results_are_merged_in_chunk_order(self) -> None:
        """
        完成顺序被打乱时，重复问题仍应保留**最靠前**的那个分块的版本。

        这是"按分块序号归并"的直接证据：如果按完成顺序归并，
        慢的第1块会被快的第3块抢先，结论就会随网络抖动变化。
        """

        self.env(PRA_AUDIT_CONCURRENCY=3, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)

        texts = _label_text()
        labels: dict[int, str] = {}

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            # 第1块最慢、第3块最快：完成顺序与分块顺序相反
            time.sleep({1: 0.15, 2: 0.05, 3: 0.0}.get(index, 0.0))
            label = _first_label(prompt)
            labels[index] = label
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "术语使用",
                        "severity": "medium",
                        # 三块报的是同一件事（同一段文字），应当被合并成一条
                        "problem": "术语前后不一致，同一个概念用了两种写法",
                        "evidence": texts.get(label, ""),
                        "suggestion": "统一术语",
                    }
                ]
            )

        update = self.run_audit({AUDIT_HEADER: respond})
        findings = update["audit_findings"]

        self.assertEqual(len(findings), 1, "三块报的同一件事应当合并成一条")
        self.assertEqual(
            findings[0]["span_label"],
            labels[1],
            "合并后应当保留分块序号最小的那一条",
        )


class TestCrossCheck(AuditConcurrencyTestCase):
    def test_duplicate_groups_are_merged(self) -> None:
        """跨批对齐：模型指出 #1、#2是同一件事，应当合并成一条。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_WHOLE_PAPER=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index == 1:
                problem = "参考文献格式不统一，缺少出版信息"
            elif index == 2:
                problem = "引文著录方式前后不一致，有的缺年份"
            else:
                return audit_response([])
            label = _first_label(prompt)
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "学术规范",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        # 先确认：关掉跨批对齐时，这两条会各自留下
        # （措辞差异大到不会被文本相似度合并）
        self.env(PRA_AUDIT_CROSS_CHECK=0)
        baseline = self.run_audit({AUDIT_HEADER: respond})
        self.assertEqual(len(baseline["audit_findings"]), 2)
        self.assertFalse(baseline["audit_meta"]["cross_check"])

        self.env(PRA_AUDIT_CROSS_CHECK=1)
        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                CROSS_CHECK_HEADER: cross_check_response(duplicates=[[1, 2]]),
            }
        )
        self.assertTrue(update["audit_meta"]["cross_check"])
        self.assertEqual(update["audit_meta"]["cross_check_merged"], 1)
        self.assertEqual(len(update["audit_findings"]), 1)

    def test_out_of_range_indices_are_ignored(self) -> None:
        """模型编造的编号不能被当真（否则会凭空吞掉问题）。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_WHOLE_PAPER=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 2:
                return audit_response([])
            label = _first_label(prompt)
            problem = "句子成分残缺，缺少主语" if index == 1 else "论证跳跃，缺少必要的过渡"
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                # 99越界、[1] 单条不成组、负数与字符串都不是合法编号
                CROSS_CHECK_HEADER: cross_check_response(
                    duplicates=[[99], [1], [-3], ["1", "2"], [0, 1]]
                ),
            }
        )
        self.assertEqual(update["audit_meta"]["cross_check_merged"], 0)
        self.assertEqual(len(update["audit_findings"]), 2)

    def test_global_issues_are_added(self) -> None:
        """跨批对齐可以补充"只有合起来看才发现"的问题。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_WHOLE_PAPER=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 2:
                return audit_response([])
            label = _first_label(prompt)
            problem = "句子成分残缺，缺少主语" if index == 1 else "论证跳跃，缺少必要的过渡"
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                CROSS_CHECK_HEADER: cross_check_response(
                    duplicates=[],
                    issues=[
                        {
                            "span_label": "P06",
                            "category": "逻辑结构",
                            "severity": "high",
                            "problem": "两块对同一处文本给出了互斥的修改方向",
                            "evidence": texts["P06"],
                        }
                    ],
                ),
            }
        )
        self.assertEqual(update["audit_meta"]["cross_check_added"], 1)
        sources = [issue.get("source") for issue in update["audit_findings"]]
        self.assertIn("cross_check", sources)


class TestWholePaperCheck(AuditConcurrencyTestCase):
    def test_whole_paper_issues_are_merged_into_findings(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 1:
                return audit_response([])
            label = _first_label(prompt)
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": "句子成分残缺，缺少主语",
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                WHOLE_PAPER_HEADER: whole_paper_response(
                    [
                        {
                            "span_label": "P06",
                            "category": "逻辑结构",
                            "severity": "high",
                            "location": "摘要 vs 结论",
                            "problem": "摘要说在三个数据集上验证，结论只提到两个",
                            "evidence": texts["P06"],
                            "suggestion": "统一为三个数据集",
                        }
                    ]
                ),
            }
        )

        self.assertTrue(update["audit_meta"]["whole_paper"])
        self.assertEqual(update["audit_meta"]["whole_paper_found"], 1)
        sources = [issue.get("source") for issue in update["audit_findings"]]
        self.assertIn("whole_paper", sources)

    def test_whole_paper_can_be_switched_off(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        calls: list[dict[str, Any]] = []
        update = self.run_audit(
            {
                AUDIT_HEADER: audit_response([]),
                WHOLE_PAPER_HEADER: whole_paper_response([]),
            },
            calls=calls,
        )
        self.assertFalse(update["audit_meta"]["whole_paper"])
        self.assertNotIn(WHOLE_PAPER_HEADER, [call["header"] for call in calls])


class TestGracefulDegradation(AuditConcurrencyTestCase):
    def test_broken_cross_check_keeps_chunk_findings(self) -> None:
        """跨批对齐输出坏掉时，分块的结论必须原样保留。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_WHOLE_PAPER=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 2:
                return audit_response([])
            label = _first_label(prompt)
            problem = "句子成分残缺，缺少主语" if index == 1 else "论证跳跃，缺少必要的过渡"
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                CROSS_CHECK_HEADER: "抱歉，我无法输出 JSON。",
            }
        )
        self.assertTrue(update["audit_meta"]["cross_check"])
        self.assertTrue(update["audit_meta"]["cross_check_error"])
        self.assertEqual(len(update["audit_findings"]), 2)

    def test_broken_whole_paper_keeps_chunk_findings(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0)
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 1:
                return audit_response([])
            label = _first_label(prompt)
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": "句子成分残缺，缺少主语",
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        update = self.run_audit(
            {
                AUDIT_HEADER: respond,
                WHOLE_PAPER_HEADER: "这不是 JSON",
            }
        )
        self.assertTrue(update["audit_meta"]["whole_paper"])
        self.assertTrue(update["audit_meta"]["whole_paper_error"])
        self.assertEqual(len(update["audit_findings"]), 1)

    def test_all_chunks_failing_still_marks_audit_failed(self) -> None:
        """并发不应该改变"所有分块都失败 -> audit_failed"的原有语义。"""

        self.env(PRA_AUDIT_CONCURRENCY=3, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        update = self.run_audit({AUDIT_HEADER: "不是 JSON"})

        self.assertTrue(update["audit_failed"])
        self.assertEqual(update["audit_findings"], [])
        self.assertEqual(update["audit_meta"]["failed_chunks"], update["audit_meta"]["chunks"])


class TestTwoPassesAreFused(AuditConcurrencyTestCase):
    """两路审核（分块 / 全文）必须融合成**一份**清单、只给一次人工闸门。"""

    def _chunks_reporting(self):
        texts = _label_text()

        def respond(prompt: str) -> str:
            index = _chunk_number(prompt)
            if index > 2:
                return audit_response([])
            label = _first_label(prompt)
            problem = "参考文献格式不统一，缺少出版信息" if index == 1 else "结论部分的措辞过于绝对"
            return audit_response(
                [
                    {
                        "span_label": label,
                        "category": "学术规范" if index == 1 else "结论",
                        "severity": "medium",
                        "problem": problem,
                        "evidence": texts.get(label, ""),
                    }
                ]
            )

        return respond

    def test_whole_paper_finding_is_visible_to_alignment(self) -> None:
        """
        跨批对齐必须跑在全文核查**之后**：否则它看不到全文那一路的结果，
        两路报的同一件事就只能靠文本相似度去碰运气。
        """

        self.env(PRA_AUDIT_CONCURRENCY=1)
        texts = _label_text()
        whole_problem = "摘要说在三个数据集上验证，结论只提到两个"
        cross_prompts: list[str] = []

        def whole_respond(prompt: str) -> str:
            return whole_paper_response(
                [
                    {
                        "span_label": "P06",
                        "category": "逻辑结构",
                        "severity": "high",
                        "location": "摘要 vs 结论",
                        "problem": whole_problem,
                        "evidence": texts["P06"],
                    }
                ]
            )

        def cross_respond(prompt: str) -> str:
            cross_prompts.append(prompt)
            return cross_check_response(duplicates=[[1, 3]])

        update = self.run_audit(
            {
                AUDIT_HEADER: self._chunks_reporting(),
                WHOLE_PAPER_HEADER: whole_respond,
                CROSS_CHECK_HEADER: cross_respond,
            }
        )

        # 全文那一路的问题出现在了对齐清单里（序号 #3 = 两个分块之后）
        self.assertTrue(cross_prompts, "跨批对齐没有被调用")
        self.assertIn(whole_problem, cross_prompts[0])

        # 两路被指为同一件事 -> 合并掉一条
        self.assertEqual(update["audit_meta"]["whole_paper_found"], 1)
        self.assertEqual(update["audit_meta"]["cross_check_merged"], 1)
        self.assertEqual(len(update["audit_findings"]), 2)

        # 用户只拿到一份清单，没有"分块的问题"和"全文的问题"两个列表
        self.assertNotIn("whole_paper_findings", update)
        self.assertNotIn("cross_check_findings", update)
        self.assertNotIn("chunk_findings", update)

    def test_findings_have_no_source_field_leaking_to_user(self) -> None:
        """来源只进audit_meta，不进用户看到的问题条目。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0)
        texts = _label_text()
        update = self.run_audit(
            {
                AUDIT_HEADER: audit_response([]),
                WHOLE_PAPER_HEADER: whole_paper_response(
                    [
                        {
                            "span_label": "P06",
                            "category": "逻辑结构",
                            "severity": "high",
                            "problem": "摘要与结论对数据集数量的说法不一致",
                            "evidence": texts["P06"],
                        }
                    ]
                ),
            }
        )

        from app.workflow.nodes import _public_issue

        shared = [_public_issue(issue) for issue in update["audit_findings"]]
        for issue in shared:
            self.assertNotIn("source", issue)


class TestWholePaperInput(AuditConcurrencyTestCase):
    """
    整篇兜底的输入控制。

    默认给了24000 token上限（它没有分块兜底，一次撑爆就整段丢失），
    但短文档仍然完整送审、不会被截断。
    """

    def test_short_document_is_sent_in_full(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0)
        prompts: list[str] = []

        def respond(prompt: str) -> str:
            prompts.append(prompt)
            return whole_paper_response([])

        update = self.run_audit({AUDIT_HEADER: audit_response([]), WHOLE_PAPER_HEADER: respond})

        self.assertTrue(update["audit_meta"]["whole_paper"])
        self.assertFalse(update["audit_meta"]["whole_paper_truncated"])
        self.assertEqual(len(prompts), 1)
        # 注意：prompt模板里也会出现"（此处省略约N tokens）"这句解释，
        # 所以这里匹配只有真正截断才会写进去的那种形式
        self.assertNotIn("……（此处省略约", prompts[0])
        # 整篇都在：最后一段也出现在prompt里
        self.assertIn(PARAGRAPHS[-1], prompts[0])

    def test_whole_paper_limit_still_applies_when_explicitly_set(self) -> None:
        """留了口子：显式设一个正数仍然会截断（头尾保留）。"""

        self.env(
            PRA_AUDIT_CONCURRENCY=1,
            PRA_AUDIT_CROSS_CHECK=0,
            PRA_AUDIT_WHOLE_PAPER_TOKENS=200,
        )
        prompts: list[str] = []

        def respond(prompt: str) -> str:
            prompts.append(prompt)
            return whole_paper_response([])

        update = self.run_audit({AUDIT_HEADER: audit_response([]), WHOLE_PAPER_HEADER: respond})

        self.assertTrue(update["audit_meta"]["whole_paper_truncated"])
        self.assertIn("……（此处省略约", prompts[0])


class TestTargetedChecks(AuditConcurrencyTestCase):
    """
    定向对照（主力）与整篇兜底（只在短文档或零发现时跑）。

    背景：跨章节矛盾本质是**对照**任务——摘要vs结论、摘要承诺vs各章产出、
    正文数字vs表格。把两份材料摆在一起，比"整篇送审让模型自己找该对照什么"
    便宜一个数量级，也更容易命中。
    """

    TARGETED_DOC = (
        "# 论文标题\n\n"
        "## 摘要\n\n"
        "本文提出了一种新方法，在三个公开数据集上做了消融实验，准确率达到 95%。\n\n"
        "## 3 实验\n\n"
        "我们在 Cora 与 Citeseer 上做了实验，准确率为 89%，并与基线做了对比。\n\n"
        "| 方法 | 准确率 |\n| --- | --- |\n| 本文方法 | 95.0 |\n\n"
        "## 5 结论\n\n"
        "本文在三个数据集上验证了方法，准确率达到 95%。\n"
    )

    def label_for(self, needle: str) -> str:
        for span in build_spans(self.TARGETED_DOC):
            if needle in span.text:
                return span.label
        raise AssertionError(f"找不到包含 {needle!r} 的段落")

    def run_targeted_audit(
        self,
        responses: dict[str, Any],
        *,
        document: str | None = None,
        calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        set_llm_factory(fake_factory(responses, calls=calls))
        state = {
            "run_id": "audit-targeted-test",
            "current_document": document or self.TARGETED_DOC,
            "document_version": 1,
            "user_request": "全面检查",
        }
        return audit_agent.run(state, logger=lambda message: None)

    def test_three_comparisons_run_and_merge_into_findings(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        label = self.label_for("我们在 Cora")
        prompts: list[str] = []

        def respond(prompt: str) -> str:
            prompts.append(prompt)
            return targeted_response(
                [
                    {
                        "span_label": label,
                        "category": "实验分析",
                        "severity": "high",
                        "location": "摘要 vs 实验",
                        "problem": "摘要说准确率 95%，实验一节写的是 89%",
                        "evidence": "我们在 Cora 与 Citeseer 上做了实验，准确率为 89%，并与基线做了对比。",
                        "suggestion": "统一成同一个数值",
                    }
                ]
            )

        update = self.run_targeted_audit(
            {AUDIT_HEADER: audit_response([]), TARGETED_CHECK_HEADER: respond}
        )

        meta = update["audit_meta"]
        # 摘要×结论 / 摘要承诺×各章产出 / 正文数字×表格
        keys = [item["key"] for item in meta["targeted"]]
        self.assertEqual(keys, ["abstract_conclusion", "promise_outcome", "numbers_tables"])
        self.assertEqual(meta["targeted_found"], 3)
        self.assertTrue(update["audit_findings"])
        self.assertIn(
            "targeted:abstract_conclusion", [i.get("source") for i in update["audit_findings"]]
        )

        # 对照材料里必须**同时**出现两份材料，而不是整篇
        self.assertIn("=== 材料A ===", prompts[0])
        self.assertIn("=== 材料B ===", prompts[0])

    def test_targeted_can_be_switched_off(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        self.env(PRA_AUDIT_TARGETED=0)
        calls: list[dict[str, Any]] = []
        update = self.run_targeted_audit(
            {AUDIT_HEADER: audit_response([]), TARGETED_CHECK_HEADER: targeted_response([])},
            calls=calls,
        )
        self.assertEqual(update["audit_meta"]["targeted"], [])
        self.assertNotIn(TARGETED_CHECK_HEADER, [call["header"] for call in calls])

    def test_broken_targeted_keeps_chunk_findings(self) -> None:
        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_WHOLE_PAPER=0)
        label = self.label_for("我们在 Cora")
        texts = {span.label: span.text for span in build_spans(self.TARGETED_DOC)}

        def chunk_respond(prompt: str) -> str:
            match = re.search(r"\[(P\d+)\]", prompt)
            current = match.group(1) if match else label
            return audit_response(
                [
                    {
                        "span_label": current,
                        "category": "语言表达",
                        "severity": "medium",
                        "problem": "句子成分残缺，缺少主语",
                        "evidence": texts.get(current, ""),
                    }
                ]
            )

        update = self.run_targeted_audit(
            {AUDIT_HEADER: chunk_respond, TARGETED_CHECK_HEADER: "这不是 JSON"}
        )

        meta = update["audit_meta"]
        self.assertTrue(all(item["error"] for item in meta["targeted"]))
        self.assertEqual(meta["targeted_found"], 0)
        self.assertTrue(update["audit_findings"], "分块的结论必须原样保留")

    def test_whole_paper_skipped_for_long_documents_with_findings(self) -> None:
        """文档不短、且已经有发现时，不再整篇送审（省掉最贵的一次调用）。"""

        self.env(
            PRA_AUDIT_CONCURRENCY=1,
            PRA_AUDIT_CROSS_CHECK=0,
            PRA_AUDIT_TARGETED=0,
            PRA_AUDIT_WHOLE_PAPER_SMALL_DOC=1,
        )
        calls: list[dict[str, Any]] = []
        texts = {span.label: span.text for span in build_spans(self.TARGETED_DOC)}
        label = self.label_for("我们在 Cora")

        update = self.run_targeted_audit(
            {
                AUDIT_HEADER: audit_response(
                    [
                        {
                            "span_label": label,
                            "category": "语言表达",
                            "severity": "medium",
                            "problem": "句子成分残缺，缺少主语",
                            "evidence": texts[label],
                        }
                    ]
                ),
                WHOLE_PAPER_HEADER: whole_paper_response([]),
            },
            calls=calls,
        )

        self.assertEqual(update["audit_meta"]["whole_paper_mode"], "skipped")
        self.assertFalse(update["audit_meta"]["whole_paper"])
        self.assertNotIn(WHOLE_PAPER_HEADER, [call["header"] for call in calls])

    def test_whole_paper_runs_as_fallback_when_nothing_was_found(self) -> None:
        """一条问题都没发现时，整篇兜底仍然跑（这是它保留下来的价值）。"""

        self.env(
            PRA_AUDIT_CONCURRENCY=1,
            PRA_AUDIT_CROSS_CHECK=0,
            PRA_AUDIT_TARGETED=0,
            PRA_AUDIT_WHOLE_PAPER_SMALL_DOC=1,
        )
        calls: list[dict[str, Any]] = []
        texts = {span.label: span.text for span in build_spans(self.TARGETED_DOC)}
        label = self.label_for("本文在三个数据集上验证")

        update = self.run_targeted_audit(
            {
                AUDIT_HEADER: audit_response([]),
                WHOLE_PAPER_HEADER: whole_paper_response(
                    [
                        {
                            "span_label": label,
                            "category": "逻辑结构",
                            "severity": "high",
                            "location": "摘要 vs 结论",
                            "problem": "摘要说做了消融实验，结论没有提",
                            "evidence": texts[label],
                        }
                    ]
                ),
            },
            calls=calls,
        )

        self.assertEqual(update["audit_meta"]["whole_paper_mode"], "fallback")
        self.assertTrue(update["audit_meta"]["whole_paper"])
        self.assertIn(WHOLE_PAPER_HEADER, [call["header"] for call in calls])
        self.assertIn("whole_paper", [i.get("source") for i in update["audit_findings"]])

    def test_small_document_still_goes_whole_paper(self) -> None:
        """短文档直接整篇送（一次调用最省事），不看有没有发现。"""

        self.env(PRA_AUDIT_CONCURRENCY=1, PRA_AUDIT_CROSS_CHECK=0, PRA_AUDIT_TARGETED=0)
        calls: list[dict[str, Any]] = []
        update = self.run_targeted_audit(
            {AUDIT_HEADER: audit_response([]), WHOLE_PAPER_HEADER: whole_paper_response([])},
            calls=calls,
        )
        self.assertEqual(update["audit_meta"]["whole_paper_mode"], "small_doc")
        self.assertIn(WHOLE_PAPER_HEADER, [call["header"] for call in calls])


class TestAuditCheckDefaults(unittest.TestCase):
    """默认值的意图要固定下来，否则以后调参时很容易把成本再放大回去。"""

    def setUp(self) -> None:
        for key in (
            "PRA_AUDIT_TARGETED",
            "PRA_AUDIT_WHOLE_PAPER",
            "PRA_AUDIT_WHOLE_PAPER_TOKENS",
            "PRA_AUDIT_WHOLE_PAPER_SMALL_DOC",
        ):
            os.environ.pop(key, None)
        reset_settings()
        self.settings = get_settings()

    def tearDown(self) -> None:
        reset_settings()

    def test_targeted_is_on_by_default(self) -> None:
        self.assertTrue(self.settings.context.audit_targeted)

    def test_whole_paper_has_a_token_cap_by_default(self) -> None:
        # 0 = 不限会把长论文的一次调用直接撑爆（它没有分块兜底）
        self.assertEqual(self.settings.context.audit_whole_paper_tokens, 24000)

    def test_small_document_threshold(self) -> None:
        # 20000：本项目的论文正文约1.7万token，这样它每轮都能整篇送一次。
        # 整篇是唯一"不依赖我们枚举配对范围"的覆盖（定向对照只能看我们想到的配对），
        # 约占总token的11%，值得留。
        self.assertEqual(self.settings.context.audit_whole_paper_small_doc, 20000)


class TestDuplicateGroupHelper(unittest.TestCase):
    """直接测编号合并这个纯函数：它是"模型只能指认、不能改写"的保证。"""

    def test_keeps_lowest_index_and_merges_severity(self) -> None:
        from app.agents.audit import _apply_duplicate_groups

        issues = [
            {"severity": "low", "suggestion": "", "problem": "a"},
            {"severity": "high", "suggestion": "补上", "problem": "b"},
            {"severity": "medium", "problem": "c"},
        ]
        merged, count = _apply_duplicate_groups(issues, [[2, 1]])

        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["severity"], "high")
        self.assertEqual(merged[0]["suggestion"], "补上")
        self.assertEqual(merged[1]["problem"], "c")

    def test_ignores_bogus_input(self) -> None:
        from app.agents.audit import _apply_duplicate_groups

        issues = [{"severity": "medium", "problem": "a"}, {"severity": "medium", "problem": "b"}]
        for bogus in (None, "x", [1], [[1]], [[1, "2"]], [[0, 9]], [[3, 4]]):
            merged, count = _apply_duplicate_groups(issues, bogus)
            self.assertEqual(count, 0, f"不该合并：{bogus!r}")
            self.assertEqual(len(merged), 2)

    def test_each_index_is_used_at_most_once(self) -> None:
        from app.agents.audit import _apply_duplicate_groups

        issues = [{"severity": "medium", "problem": str(i)} for i in range(4)]
        merged, count = _apply_duplicate_groups(issues, [[1, 2], [2, 3]])

        # #2已经归进第一组，第二组只剩 #3一条 -> 不成组，不合并
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 3)
