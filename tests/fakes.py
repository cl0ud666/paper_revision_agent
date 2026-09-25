"""
测试用的假 LLM。

不联网也能把整条 Workflow 跑通：
根据 Prompt 里的【任务】标记，返回预置的 JSON。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.agents.prompts import (
    AUDIT_HEADER,
    ORCHESTRATOR_HEADER,
    REVIEW_HEADER,
    REVISION_HEADER,
)
from evals.judges import JUDGE_HEADER

HEADERS = (ORCHESTRATOR_HEADER, AUDIT_HEADER, REVISION_HEADER, REVIEW_HEADER, JUDGE_HEADER)


class FakeMessage:
    """模拟 LangChain 的 AIMessage。"""

    def __init__(self, content: str, prompt_tokens: int = 100, completion_tokens: int = 50):
        self.content = content
        self.usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        self.response_metadata: dict[str, Any] = {}


def flatten_messages(messages: Any) -> str:
    """把 invoke 的 messages 参数拍平成文本。"""

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

    responses: {header: 内容 或 callable(prompt) -> 内容}
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
    """构造一个可以塞进 set_llm_factory 的工厂函数。"""

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


def patch_response(patches: list[dict[str, Any]], notes: str = "") -> str:
    """Revision Agent 的输出：一次给出本轮全部补丁。"""

    return json.dumps({"patches": patches, "notes": notes}, ensure_ascii=False)


def revision_response(patches: list[dict[str, Any]], notes: str = "") -> str:
    """兼容旧写法（现在 Revision 输出的是补丁）。"""

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
