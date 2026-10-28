"""
测试用的假LLM。

不联网也能把整条Workflow跑通：
根据Prompt里的【任务】标记，返回预置的JSON。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.agents.prompts import (
    AUDIT_HEADER,
    CROSS_CHECK_HEADER,
    NAME_CHECK_HEADER,
    ORCHESTRATOR_HEADER,
    REVIEW_HEADER,
    REVISION_HEADER,
    TARGETED_CHECK_HEADER,
    WHOLE_PAPER_HEADER,
)
from evals.judges import JUDGE_HEADER

HEADERS = (
    ORCHESTRATOR_HEADER,
    AUDIT_HEADER,
    NAME_CHECK_HEADER,
    CROSS_CHECK_HEADER,
    TARGETED_CHECK_HEADER,
    WHOLE_PAPER_HEADER,
    REVISION_HEADER,
    REVIEW_HEADER,
    JUDGE_HEADER,
)


class FakeMessage:
    """模拟LangChain的AIMessage。"""

    def __init__(self, content: str, prompt_tokens: int = 100, completion_tokens: int = 50):
        self.content = content
        self.usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        self.response_metadata: dict[str, Any] = {}


def flatten_messages(messages: Any) -> str:
    """把invoke的messages参数拍平成文本。"""

    parts: list[str] = []
    for message in messages:
        if isinstance(message, tuple):
            parts.append(str(message[1]))
        else:
            parts.append(str(getattr(message, "content", message)))
    return "\n".join(parts)


def detect_header(text: str) -> str:
    for header in HEADERS:
        if header in text:
            return header
    return ""


class FakeChatModel:
    """
    按【任务】标记返回预置内容的假模型。

    responses: {header: 内容 或callable(prompt) -> 内容}
    """

    def __init__(
        self,
        responses: dict[str, Any],
        *,
        calls: list[dict[str, Any]] | None = None,
        model: str = "fake-model",
    ) -> None:
        self.responses = responses
        self.calls = calls if calls is not None else []
        self.model = model

    def invoke(self, messages: Any, **kwargs: Any) -> FakeMessage:
        prompt = flatten_messages(messages)
        header = detect_header(prompt)
        self.calls.append({"header": header, "prompt": prompt})

        response = self.responses.get(header)
        if response is None:
            response = self.responses.get("*", "{}")
        content = response(prompt) if callable(response) else response
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return FakeMessage(content)


def fake_factory(
    responses: dict[str, Any],
    *,
    calls: list[dict[str, Any]] | None = None,
) -> Callable[[str, dict[str, Any]], FakeChatModel]:
    """构造一个可以塞进set_llm_factory的工厂函数。"""

    def factory(agent: str, overrides: dict[str, Any]) -> FakeChatModel:
        return FakeChatModel(responses, calls=calls)

    return factory


def plan_response(next_action: str = "audit") -> str:
    return json.dumps(
        {
            "plan_kind": "full_review" if next_action == "audit" else "direct_revision",
            "next_action": next_action,
            "plan": ["执行下一步"],
            "focus_areas": ["语言表达"],
            "reason": "测试用规划",
            "instructions": "请只处理已确认的问题。",
        },
        ensure_ascii=False,
    )


def audit_response(issues: list[dict[str, Any]]) -> str:
    return json.dumps({"issues": issues}, ensure_ascii=False)


def name_check_response(suspected: list[dict[str, Any]]) -> str:
    """专名核验的输出（写法本身对不对）。"""

    return json.dumps({"suspected": suspected}, ensure_ascii=False)


def cross_check_response(
    duplicates: list[list[int]] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> str:
    """跨批对齐的输出：哪些候选是同一件事 + 只有全局视角才发现的问题。"""

    return json.dumps(
        {"duplicates": duplicates or [], "issues": issues or []},
        ensure_ascii=False,
    )


def whole_paper_response(issues: list[dict[str, Any]]) -> str:
    """全文矛盾核查的输出（不分块，整篇送审）。"""

    return json.dumps({"issues": issues}, ensure_ascii=False)


def targeted_response(issues: list[dict[str, Any]]) -> str:
    """定向对照的输出（把两份材料摆到一起查矛盾）。"""

    return json.dumps({"issues": issues}, ensure_ascii=False)


def patch_response(patches: list[dict[str, Any]], notes: str = "") -> str:
    """Revision Agent的输出：一次给出本轮全部补丁。"""

    return json.dumps({"patches": patches, "notes": notes}, ensure_ascii=False)


def revision_response(patches: list[dict[str, Any]], notes: str = "") -> str:
    """兼容旧写法（现在Revision输出的是补丁）。"""

    return patch_response(patches, notes)


def review_response(status: str, **extra: Any) -> str:
    payload = {
        "status": status,
        "summary": "测试用复核结论",
        "per_issue": [],
        "remaining_issues": [],
        "constraint_violations": [],
        "dimension_scores": {"语法": 4, "原意": 5, "学术规范": 4, "降重": 4},
        "confidence": 0.8,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)
