"""Export static prompt templates only; never include user documents or secrets.

Run: python -m scripts.export_prompts
"""
from pathlib import Path
from app.agents import prompts


def main():
    roles = [
        ("编排 / Orchestrator", "ORCHESTRATOR", prompts.render_orchestrator_prompt,
         {"is_replan": False, "max_iterations": 3, "turn": 1}),
        ("局部审查 / Audit", "AUDIT", prompts.render_audit_prompt, {}),
        ("专名核验 / Name Check", "NAME_CHECK", prompts.render_name_check_prompt, {}),
        ("跨批对齐 / Cross Check", "CROSS_CHECK", prompts.render_cross_check_prompt, {}),
        ("全文核查 / Whole Paper", "WHOLE_PAPER", prompts.render_whole_paper_prompt, {}),
        ("定向对照 / Targeted Check", "TARGETED_CHECK", prompts.render_targeted_check_prompt, {}),
        ("生成补丁 / Revision", "REVISION", prompts.render_patch_prompt, {}),
        ("独立复核 / Review", "REVIEW", prompts.render_review_prompt, {}),
    ]
    lines = ["# 当前 Prompt 原文导出", "",
             "来源：`app/agents/prompts.py`。仅导出静态模板，不读取运行记录、密钥或论文。",
             "`{{CONTEXT}}` 是运行时上下文占位符；编排示例参数为第 1 轮、上限 3 次。",
             "整理与改写提案见 `prompt_redesign.md`。本文件不是已启用的新配置。", ""]
    for label, prefix, render, kwargs in roles:
        system = getattr(prompts, prefix + "_SYSTEM")
        template = render("{{CONTEXT}}", **kwargs)
        lines.extend([f"## {label}", "", f"静态字符数：System {len(system)}；User {len(template)}。", "",
                      "### System", "", "```text", system, "```", "", "### User", "", "```text", template.rstrip(), "```", ""])
    lines.extend(["## 编排的重规划分支", "", "```text",
                  prompts.render_orchestrator_prompt("{{CONTEXT}}", is_replan=True, max_iterations=3, turn=2).rstrip(),
                  "```", ""])
    target = Path(__file__).resolve().parents[1] / "docs" / "prompts_current.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
