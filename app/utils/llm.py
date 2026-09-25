"""
LLM 调用封装。

职责：

1. 从 .env 读取 API Key，创建统一的 ChatModel 实例（默认 DeepSeek）；
2. 提供 call_llm() 统一入口，返回带 Token 用量的 LLMResponse；
3. 支持测试注入（set_llm_factory），评测与单元测试不需要真实网络。

重试 / 超时 / 熔断不在这里做，而是由 app.utils.resilience.AgentGuard 负责，
这样“一次 LLM 调用”和“一个 Agent 步骤”的职责是分开的。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.config import get_settings


class LLMConfigurationError(RuntimeError):
    """LLM 配置问题（例如缺少 API Key）。"""


@dataclass
class LLMUsage:
    """Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """一次 LLM 调用的结果。"""

    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    raw: Any = None


# 测试 / 评测可以注入自己的工厂：factory(agent, overrides) -> chat model
_FACTORY: Callable[[str, dict[str, Any]], Any] | None = None
_FACTORY_LOCK = threading.Lock()


def set_llm_factory(factory: Callable[[str, dict[str, Any]], Any] | None) -> None:
    """注入自定义 LLM 工厂（单元测试 / 离线评测用）。"""

    global _FACTORY
    with _FACTORY_LOCK:
        _FACTORY = factory


def reset_llm_factory() -> None:
    """恢复真实 LLM。"""

    set_llm_factory(None)


def get_chat_model(
    agent: str = "default",
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool | None = None,
) -> Any:
    """创建一个 ChatModel 实例。"""

    settings = get_settings()
    json_mode = settings.llm.json_mode if json_mode is None else json_mode
    temperature = (
        settings.agents.temperature_for(agent) if temperature is None else temperature
    )
    max_tokens = settings.llm.max_tokens if max_tokens is None else max_tokens

    with _FACTORY_LOCK:
        factory = _FACTORY
    if factory is not None:
        return factory(
            agent,
            {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
            },
        )

    api_key = settings.llm.api_key
    if not api_key:
        raise LLMConfigurationError(
            f"没有找到环境变量 {settings.llm.api_key_env}，请在项目根目录的 .env 中配置。"
        )

    # 延迟导入：没有装 langchain-openai 时，单元测试仍然可以运行
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": settings.llm.model,
        "api_key": api_key,
        "base_url": settings.llm.base_url,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": settings.llm.timeout,
        # 重试由 AgentGuard 统一负责，这里关闭 SDK 自带重试，避免重复放大请求
        "max_retries": 0,
    }
    if json_mode:
        kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

    return ChatOpenAI(**kwargs)


def message_text(message: Any) -> str:
    """从 LangChain 消息对象中取出文本内容。"""

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
        return "".join(parts)
    return str(content)


def usage_from_message(message: Any) -> LLMUsage:
    """从消息对象中提取 Token 用量（兼容多种字段名）。"""

    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return LLMUsage(
            prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            completion_tokens=int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            ),
            total_tokens=int(usage.get("total_tokens") or 0),
        )

    metadata = getattr(message, "response_metadata", None) or {}
    token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if isinstance(token_usage, dict) and token_usage:
        prompt = int(token_usage.get("prompt_tokens") or 0)
        completion = int(token_usage.get("completion_tokens") or 0)
        return LLMUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(token_usage.get("total_tokens") or (prompt + completion)),
        )

    return LLMUsage()


def _looks_like_json_mode_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("response_format", "json_object", "json mode"))


def call_llm(
    prompt: str,
    agent: str = "default",
    *,
    system: str | None = None,
    json_mode: bool | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    run_id: str = "",
    action: str = "",
) -> LLMResponse:
    """
    调用 LLM 并返回结构化结果。

    如果服务端不支持 response_format=json_object，会自动降级重试一次。
    """

    messages: list[tuple[str, str]] = []
    if system:
        messages.append(("system", system))
    messages.append(("human", prompt))

    model = get_chat_model(
        agent, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
    )

    try:
        message = model.invoke(messages)
    except Exception as exc:  # noqa: BLE001
        if (json_mode is None or json_mode) and _looks_like_json_mode_error(exc):
            fallback = get_chat_model(
                agent, temperature=temperature, max_tokens=max_tokens, json_mode=False
            )
            message = fallback.invoke(messages)
        else:
            raise

    response = LLMResponse(
        text=message_text(message),
        usage=usage_from_message(message),
        model=get_settings().llm.model,
        raw=message,
    )
    _log_call(
        run_id=run_id,
        agent=agent,
        action=action,
        system=system or "",
        prompt=prompt,
        response=response.text,
        usage=response.usage,
    )
    return response


def _log_call(
    *,
    run_id: str,
    agent: str,
    action: str,
    system: str,
    prompt: str,
    response: str,
    usage: LLMUsage,
) -> None:
    """
    把每次模型调用完整落盘：data/runs/<run_id>/llm_calls.jsonl。

    用途：回查“这个 Agent 到底看到了什么、模型回了什么”，
    排查定位失败、理由不合理之类的问题时特别有用。
    """

    settings = get_settings()
    if not settings.llm.log_calls or not run_id:
        return

    limit = settings.llm.log_prompt_chars
    record = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "agent": agent,
        "action": action,
        "model": settings.llm.model,
        "system": system[:limit] if limit else system,
        "prompt": prompt[:limit] if limit else prompt,
        "response": response[:limit] if limit else response,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    try:
        path = Path(settings.paths.run_dir(run_id)) / "llm_calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def ping(timeout: float | None = None) -> LLMResponse:
    """
    连通性自检：发一个极小的请求，验证 API Key / 网络 / 模型名是否正确。
    """

    return call_llm(
        "请只输出 JSON：{\"ok\": true}",
        agent="review",
        json_mode=True,
        max_tokens=32,
        temperature=0.0,
    )


def describe_configuration() -> dict[str, Any]:
    """返回当前 LLM 配置摘要（不包含密钥本身）。"""

    settings = get_settings()
    api_key = settings.llm.api_key or ""
    masked = f"{api_key[:6]}…{api_key[-4:]}" if len(api_key) > 12 else ("已配置" if api_key else "")
    return {
        "model": settings.llm.model,
        "base_url": settings.llm.base_url,
        "api_key_env": settings.llm.api_key_env,
        "api_key": masked,
        "json_mode": settings.llm.json_mode,
        "temperature_by_agent": {
            "orchestrator": settings.agents.orchestrator_temperature,
            "audit": settings.agents.audit_temperature,
            "revision": settings.agents.revision_temperature,
            "review": settings.agents.review_temperature,
        },
    }


def dumps(value: Any) -> str:
    """调试用：紧凑 JSON。"""

    return json.dumps(value, ensure_ascii=False, default=str)
