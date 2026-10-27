"""
LLM 连通性自检脚本。

用途：确认 .env 里的 API Key、网络与模型名都正确。

运行：

    python -m scripts.diagnostics.check_llm
"""

from __future__ import annotations

from app.utils.llm import describe_configuration, ping


def main() -> int:
    print("========== LLM 配置 ==========")
    for key, value in describe_configuration().items():
        print(f"{key}: {value}")

    print("\n========== 连通性检查 ==========")
    try:
        response = ping()
    except Exception as exc:  # noqa: BLE001 - 自检脚本需要给出可读的错误
        print(f"失败：{type(exc).__name__}: {exc}")
        return 1

    print("成功。模型返回：")
    print(response.text.strip())
    print(
        f"\nToken 用量：prompt={response.usage.prompt_tokens} "
        f"completion={response.usage.completion_tokens} total={response.usage.total_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
