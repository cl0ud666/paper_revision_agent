"""
LLM-as-Judge：对“修改前 / 修改后”的文本对做多维度打分。

四个维度（与项目总设计一致）：

1. 语法：语言是否通顺、无语法错误
2. 原意：是否保持了作者原意与关键信息
3. 学术规范：表述是否客观、规范
4. 降重：是否减少冗余重复

评分为1~5的整数（5最好）。
"""

from __future__ import annotations

from functools import partial
from typing import Any

from app.utils.json_utils import ModelOutputError, parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.text import compact_text

DIMENSIONS = ("语法", "原意", "学术规范", "降重")

JUDGE_HEADER = "【任务】JUDGE质量评分"

JUDGE_SYSTEM = (
    "你是学术写作评测专家。"
    "你需要对“修改前 / 修改后”的文本对做客观评分，"
    "评分必须基于文本本身，不能因为修改更“华丽”就给高分。"
    "始终只输出合法JSON。"
)


def render_judge_prompt(
    *,
    original: str,
    revised: str,
    focus: str = "",
    expectation: str = "",
    must_keep: list[str] | None = None,
) -> str:
    must_keep_text = "、".join(must_keep or []) or "（未特别指定）"
    return f"""{JUDGE_HEADER}

# 评分任务
请比较下面的“修改前”和“修改后”，按四个维度打分（1~5的整数，5最好）。

重点考察维度：{focus or "综合"}
期望的修改效果：{expectation or "（未特别指定）"}
必须保留的信息：{must_keep_text}

# 修改前
{original}

# 修改后
{revised}

# 评分标准
- 语法：是否消除语法错误、表达是否通顺。
- 原意：是否保持原意与关键信息（数字、条件、限定语、结论强度）。
  仅仅“变得更学术”但改变了原意，应给低分。
- 学术规范：是否客观、规范，是否去掉口语化与无依据的断言。
- 降重：是否消除冗余重复（如果没有冗余，保持原样也应得高分）。

# 输出格式（只输出JSON）
{{
  "语法": 1,
  "原意": 1,
  "学术规范": 1,
  "降重": 1,
  "preserved_meaning": true,
  "changed_facts": ["被改变的原文信息，没有则空数组"],
  "reason": "一句话说明评分依据"
}}
"""


def judge_pair(
    original: str,
    revised: str,
    *,
    focus: str = "",
    expectation: str = "",
    must_keep: list[str] | None = None,
    recorder: Any | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """对一组文本对打分。"""

    prompt = render_judge_prompt(
        original=compact_text(original, 3000),
        revised=compact_text(revised, 3000),
        focus=focus,
        expectation=expectation,
        must_keep=must_keep,
    )

    guard = get_guard("judge")
    response = guard.run(
        partial(
            call_llm,
            prompt,
            "judge",
            system=JUDGE_SYSTEM,
            json_mode=True,
            run_id=run_id,
            action="judge",
        ),
        action="judge",
        recorder=recorder,
        run_id=run_id,
    )

    try:
        data = parse_json_object(response.text, required_keys=("语法",), context="Judge")
    except ModelOutputError:
        return _failed_result("Judge输出无法解析")

    scores: dict[str, int] = {}
    for dimension in DIMENSIONS:
        value = data.get(dimension)
        try:
            scores[dimension] = max(1, min(5, int(float(value))))
        except (TypeError, ValueError):
            scores[dimension] = 0

    return {
        "scores": scores,
        "average": round(sum(scores.values()) / len(DIMENSIONS), 3),
        "preserved_meaning": bool(data.get("preserved_meaning", True)),
        "changed_facts": [str(item) for item in (data.get("changed_facts") or [])],
        "reason": str(data.get("reason") or ""),
        "error": "",
    }


def _failed_result(message: str) -> dict[str, Any]:
    return {
        "scores": {dimension: 0 for dimension in DIMENSIONS},
        "average": 0.0,
        "preserved_meaning": False,
        "changed_facts": [],
        "reason": "",
        "error": message,
    }
