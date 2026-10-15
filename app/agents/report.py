"""
Report 节点：把整个任务的执行结果汇总成可读报告，并落盘。

这里刻意**不使用 LLM**：报告是确定性的汇总，
避免“最后的输出还要依赖一次模型调用”带来的不确定性。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from app.agents.revision import all_applied_edits, all_failed_edits
from app.config import get_settings
from app.document.docx_redline import applied_rounds, export_redline
from app.utils.resilience import guard_snapshots
from app.utils.tracing import get_recorder, utc_now

SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


def write_redline_docx(
    state: Any,
    *,
    run_dir: Path,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    把已落实的改动写回 Word 原文件，另存为带**修订痕迹 + 批注**的副本。

    为什么是"另存副本"：原文件必须一字不动地留着，
    用户要能对照、要能拒绝、要能反悔。

    写不回去的改动不会被丢掉：它们会变成文档里的批注，
    并在返回的摘要里列出来。
    """

    log = logger or print
    settings = get_settings()
    if not settings.output.docx_output:
        return {}

    source = str(state.get("source_docx") or "")
    rounds = applied_rounds(state)
    # 建议类：原文没写错（或涉及代码），不生成补丁，只在原地留一条批注。
    suggestions = [
        {**issue, "kind": "advice"}
        for issue in (state.get("audit_findings") or [])
        if (issue.get("kind") or "fix") == "advice"
    ]
    # 没有补丁、也没有建议，才没什么可写的
    if not source or (not rounds and not suggestions):
        return {}

    path = Path(source)
    if not path.exists():
        log(f"[Report] 找不到原始 Word 文件，跳过写回：{path}")
        return {}

    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "revised_document.docx"
    try:
        outcome = export_redline(
            path.read_bytes(),
            rounds,
            str(target),
            author=settings.output.redline_author,
            comments=settings.output.redline_comments,
            suggestions=suggestions,
        )
    except Exception as exc:  # noqa: BLE001 - 写回失败不该让整个任务失败
        log(f"[Report] 写回 Word 失败（{type(exc).__name__}: {exc}），只输出文字稿。")
        return {"redline_summary": {"error": f"{type(exc).__name__}: {exc}"}}

    summary = outcome.to_json()
    log(f"[Report] Word 副本：{target}（{outcome.summary()}）")
    return {
        "docx_path": str(target),
        "redline_summary": summary,
    }


def _cell(value: Any) -> str:
    """把文本塞进 Markdown 表格单元格。"""

    text = str(value if value is not None else "").replace("|", "\\|")
    return text.replace("\n", "<br>").strip()


def _first_line(value: Any) -> str:
    """只取第一行（表格里放完整摘要会把报告撑得很大）。"""

    text = str(value if value is not None else "").strip()
    return text.splitlines()[0] if text else ""


def build_report(state: Any, *, trace_summary: dict[str, Any] | None = None) -> str:
    """生成 Markdown 报告。"""

    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    review_result = state.get("review_result") or {}
    findings = state.get("audit_findings") or []
    approved_ids = set(state.get("approved_issue_ids") or [])
    # 汇总所有轮次的修改（revision_edits 只保留最后一轮）
    edits = all_applied_edits(state)
    failed_edits = all_failed_edits(state)
    history = state.get("revision_history") or []

    lines: list[str] = []
    lines.append("# 学术论文修改报告")
    lines.append("")
    lines.append(f"- 运行编号：`{run_id}`")
    lines.append(f"- 生成时间：{utc_now()}")
    lines.append(f"- 使用模型：`{settings.llm.model}`")
    lines.append(f"- 用户任务：{state.get('user_request') or '（未提供）'}")
    lines.append(
        f"- 执行路径：{state.get('plan_kind') or '-'}"
        f"（当前动作：{state.get('next_action') or '-'}）"
    )
    lines.append(
        f"- 迭代轮次：{state.get('iteration', 0)} / {state.get('max_iterations', 3)}"
    )
    lines.append(f"- 最终结论：{_final_conclusion(state)}")
    redline = state.get("redline_summary") or {}
    if state.get("docx_path"):
        lines.append(f"- Word 副本（带修订痕迹）：`{state['docx_path']}`")
        if redline.get("skipped"):
            lines.append(
                f"- 其中 {redline.get('skipped')} 条改动无法自动写回 Word，"
                "已在原位留批注，需要人工处理（见第四节末尾）"
            )
    lines.append("")

    # ---------- 计划 ----------
    lines.append("## 一、执行计划（Orchestrator）")
    lines.append("")
    plan = state.get("plan") or []
    if plan:
        for position, step in enumerate(plan, start=1):
            lines.append(f"{position}. {step}")
    else:
        lines.append("（无）")
    if state.get("plan_reason"):
        lines.append("")
        lines.append(f"规划理由：{state['plan_reason']}")
    lines.append("")

    # ---------- Audit ----------
    lines.append("## 二、发现的问题（Audit Agent）")
    lines.append("")
    meta = state.get("audit_meta") or {}
    if meta:
        lines.append(
            f"审查片段 {meta.get('chunks', 0)} 个（失败 {meta.get('failed_chunks', 0)} 个），"
            f"原始问题 {meta.get('raw_issues', 0)} 条，合并后 {meta.get('merged_issues', 0)} 条。"
        )
        if meta.get("chunks_dropped"):
            lines.append(
                f"⚠ 受上限限制，本次只审到前 {meta.get('chunks', 0)} 批"
                f"（整篇共 {meta.get('chunks_total', 0)} 批），"
                f"后面 {meta.get('chunks_dropped')} 批**没有审**；"
                "需要审全文请调大 `PRA_AUDIT_MAX_CHUNKS` 或 `PRA_AUDIT_CHUNK_TOKENS`。"
            )
        lines.append("")
    if findings:
        applied_issue_ids = {
            patch.get("issue_id") for patch in (state.get("applied_patches") or [])
        }
        proposed_issue_ids = {
            patch.get("issue_id") for patch in (state.get("proposed_patches") or [])
        }
        lines.append("| ID | 严重程度 | 分类 | 位置 | 问题 | 是否修改 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for issue in findings:
            if issue.get("id") in applied_issue_ids:
                handled = "已修改"
            elif issue.get("id") in proposed_issue_ids:
                handled = "待决定"
            elif issue.get("id") in approved_ids:
                handled = "已批准"
            else:
                handled = "否"
            lines.append(
                "| {id} | {severity} | {category} | {location} | {problem} | {approved} |".format(
                    id=_cell(issue.get("id")),
                    severity=_cell(SEVERITY_LABEL.get(issue.get("severity", ""), issue.get("severity"))),
                    category=_cell(issue.get("category")),
                    location=_cell(issue.get("location")),
                    problem=_cell(issue.get("problem")),
                    approved=handled,
                )
            )
    else:
        lines.append("（未发现问题，或未执行审查）")
    lines.append("")

    # ---------- 人机协作 ----------
    lines.append("## 三、人机协作（Human Approval）")
    lines.append("")
    rounds = state.get("human_rounds") or []
    patch_rounds = [entry for entry in rounds if entry.get("type") == "patch_review"]
    if patch_rounds:
        accepted_total = sum(len(entry.get("accepted") or []) for entry in patch_rounds)
        rejected_total = sum(len(entry.get("rejected") or []) for entry in patch_rounds)
        lines.append(
            f"- 补丁审阅：共 {len(patch_rounds)} 轮，用户接受 {accepted_total} 条、"
            f"拒绝 {rejected_total} 条（逐条决定，理由由 Agent 提供）"
        )
    if state.get("human_decision"):
        lines.append(f"- 问题范围确认：{state['human_decision']}")
    if not patch_rounds and not state.get("human_decision"):
        lines.append("- （未进入人工确认环节）")
    # 用户要求与决定：这是多轮里唯一需要长期记住的东西
    user_rules = state.get("user_rules") or []
    if user_rules:
        lines.append("- 用户要求与决定（带编号，跨轮次有效）：")
        for rule in user_rules:
            kind = "要求" if rule.get("kind") == "rule" else "明确不改"
            revoked = "（已撤销）" if rule.get("status", "active") != "active" else ""
            lines.append(
                f"  - [{_cell(rule.get('id'))}][{kind}] {_cell(rule.get('text'))}"
                f"{revoked}（第 {rule.get('created_turn', 1)} 轮提出）"
            )
    ignored_issues = [issue for issue in findings if issue.get("ignored")]
    if ignored_issues:
        lines.append(
            "- 按用户要求忽略的问题："
            + "；".join(
                f"#{issue.get('id')} {issue.get('span_label') or ''} "
                f"{_cell((issue.get('problem') or '')[:40])}"
                for issue in ignored_issues
            )
        )
    constraints = state.get("human_constraints") or []
    if constraints:
        lines.append("- 用户约束：")
        for constraint in constraints:
            lines.append(f"  - {constraint}")
    else:
        lines.append("- 用户约束：（无）")
    if state.get("human_feedback"):
        lines.append(f"- 补充说明：{state['human_feedback']}")
    if rounds:
        lines.append(f"- 人工确认轮次：{len(rounds)}")
    lines.append("")

    # ---------- Revision ----------
    lines.append("## 四、修改内容（Revision Agent）")
    lines.append("")
    lines.append(state.get("revision_summary") or "（本轮没有执行修改）")
    lines.append("")
    if history:
        lines.append("各轮执行情况：")
        lines.append("")
        stage_labels = {"propose": "提出补丁", "apply": "应用补丁"}
        lines.append("| 轮次 | 阶段 | 落实修改 | 未落实 | 摘要 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in history:
            lines.append(
                f"| 第 {_cell(entry.get('iteration'))} 轮 | "
                f"{stage_labels.get(entry.get('stage', ''), '修改')} | "
                f"{_cell(entry.get('edits'))} | {_cell(entry.get('failed'))} | "
                f"{_cell(_first_line(entry.get('summary')))} |"
            )
        lines.append("")
    if edits:
        lines.append(
            "| # | 补丁 | 轮次 | 段落 | 修改前 | 修改后 | 修改理由（由 Agent 给出） | 决定 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for position, edit in enumerate(edits, start=1):
            lines.append(
                "| {n} | {patch} | {round} | {span} | {before} | {after} | {rationale} | {decision} |".format(
                    n=position,
                    patch=_cell(edit.get("patch_id")),
                    round=_cell(edit.get("round", "")),
                    span=_cell(edit.get("position") or edit.get("span_label") or edit.get("span_id")),
                    before=_cell(edit.get("before") or edit.get("original")),
                    after=_cell(edit.get("replace") or edit.get("revised")),
                    rationale=_cell(edit.get("rationale")),
                    decision=_cell(edit.get("decided_by") or "user"),
                )
            )
        lines.append("")

    # 用户的逐条决定（补丁审阅结果）
    stats = state.get("patch_stats") or {}
    rejected = state.get("rejected_patches") or []
    if stats or rejected:
        lines.append(
            "补丁决定统计："
            f"接受 {stats.get('accepted', len(edits))} 条、"
            f"拒绝 {stats.get('rejected', len(rejected))} 条、"
            f"未能落实 {stats.get('conflict', 0)} 条（原因见上表；"
            "含无法定位 / 定位有歧义 / 无效改动 / 改动冲突）。"
        )
        lines.append("")
    if rejected:
        lines.append("被拒绝的改动（不会出现在论文里，下一轮也不会重复提出）：")
        lines.append("")
        for patch in rejected:
            lines.append(
                f"- {patch.get('patch_id', '')} "
                f"{patch.get('span_label') or patch.get('span_id') or ''}："
                f"「{_cell((patch.get('before') or '')[:60])}」→"
                f"「{_cell((patch.get('replace') or '')[:60])}」"
                f"｜Agent 给的理由：{_cell((patch.get('rationale') or '—')[:80])}"
            )
        lines.append("")

    # 写不回 Word 的改动：这些必须在 Word 里手工处理，所以单独列出来
    skipped_items = (state.get("redline_summary") or {}).get("skipped_items") or []
    if skipped_items:
        lines.append("### 需要在 Word 里人工处理的改动")
        lines.append("")
        lines.append(
            "下面这些改动已经通过审核，但没办法安全地写回 Word"
            "（多与公式、图片、跨单元格或排版有关），"
            "已在文档对应段落留下批注（作者：论文修改助手），请在 Word 里手工确认。"
        )
        lines.append("")
        lines.append("| 补丁 | 位置 | 原文 | 建议改为 | 为什么没自动改 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in skipped_items:
            position = item.get("position") or ""
            location = str(item.get("location") or "").strip()
            if location and location not in str(position):
                position = f"{position}（{location}）" if position else location
            after = str(item.get("after") or "").strip()
            lines.append(
                f"| {_cell(item.get('patch_id'))} | {_cell(position)} "
                f"| {_cell(str(item.get('before') or '')[:80])} "
                f"| {_cell(after[:80] if after else '（删除）')} "
                f"| {_cell(item.get('reason'))} |"
            )
        lines.append("")

    if failed_edits:
        lines.append("未能落实的修改：")
        lines.append("")
        for edit in failed_edits:
            lines.append(
                f"- 问题 {edit.get('issue_id')}：原因 `{edit.get('reason')}`"
                f"，定位片段：{_cell(str(edit.get('find', ''))[:80])}"
            )
        lines.append("")

    # ---------- Review ----------
    lines.append("## 五、独立复核（Review Agent）")
    lines.append("")
    lines.append(f"- 结论：**{str(review_result.get('status', '未执行')).upper()}**")
    if review_result.get("summary"):
        lines.append(f"- 复核摘要：{review_result['summary']}")
    if review_result.get("confidence") is not None:
        lines.append(f"- 置信度：{review_result.get('confidence')}")
    scores = review_result.get("dimension_scores") or {}
    if scores:
        lines.append(
            "- 维度评分（1~5）："
            + "，".join(f"{key} {value}" for key, value in scores.items())
        )
    lines.append("")

    per_issue = review_result.get("per_issue") or []
    if per_issue:
        lines.append("| 问题 ID | 状态 | 判断依据 |")
        lines.append("| --- | --- | --- |")
        for item in per_issue:
            lines.append(
                f"| {_cell(item.get('id'))} | {_cell(item.get('status'))} | {_cell(item.get('reason'))} |"
            )
        lines.append("")

    remaining = review_result.get("remaining_issues") or []
    if remaining:
        lines.append("仍然存在的问题：")
        lines.append("")
        for issue in remaining:
            lines.append(
                f"- [{SEVERITY_LABEL.get(issue.get('severity', ''), issue.get('severity'))}]"
                f"（{issue.get('category')}）{issue.get('problem')}"
                f"｜位置：{issue.get('location') or '未标注'}"
                f"｜建议：{issue.get('suggestion') or '—'}"
            )
        lines.append("")

    violations = review_result.get("constraint_violations") or []
    if violations:
        lines.append("约束违背记录：")
        lines.append("")
        for violation in violations:
            lines.append(f"- {violation}")
        lines.append("")

    checks = review_result.get("checks") or []
    if checks:
        lines.append("确定性检查项：")
        lines.append("")
        for check in checks:
            lines.append(f"- {check.get('name')}：{check.get('status')}（{check.get('detail')}）")
        lines.append("")

    # ---------- Harness ----------
    lines.append("## 六、运行观测（Agent Harness）")
    lines.append("")
    lines.append(_trace_table(trace_summary))
    lines.append("")
    lines.append(_budget_table(state, trace_summary))
    lines.append("")
    breaks = guard_snapshots()
    if breaks:
        lines.append(
            "熔断器状态："
            + "，".join(f"{item['agent']}={item['state']}" for item in breaks)
        )
        lines.append("")

    errors = list(state.get("input_warnings") or []) + list(state.get("errors") or [])
    if errors:
        lines.append("## 七、异常与降级记录")
        lines.append("")
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _final_conclusion(state: Any) -> str:
    review_result = state.get("review_result") or {}
    status = str(review_result.get("status") or "").lower()
    iteration = state.get("iteration", 0)
    max_iterations = state.get("max_iterations", 3)
    if status == "pass":
        return "复核通过"
    if status == "fail" and iteration >= max_iterations:
        return "达到迭代上限，输出当前最优结果（仍有未解决问题）"
    if status == "fail":
        return "复核未通过"
    return "未执行复核"


def _trace_table(trace_summary: dict[str, Any] | None) -> str:
    if not trace_summary:
        return "（无 Trace 数据）"

    per_agent = trace_summary.get("per_agent") or {}
    if not per_agent:
        return "（无 Trace 数据）"

    lines = ["| Agent | 调用次数 | 失败 | 重试 | 总耗时(ms) | Tokens |", "| --- | --- | --- | --- | --- | --- |"]
    for agent, bucket in per_agent.items():
        lines.append(
            f"| {agent} | {bucket.get('calls', 0)} | {bucket.get('failures', 0)} | "
            f"{bucket.get('retries', 0)} | {bucket.get('total_ms', 0)} | "
            f"{bucket.get('total_tokens', 0)} |"
        )
    totals = trace_summary.get("totals") or {}
    lines.append(
        f"| **合计** | {totals.get('calls', 0)} | {totals.get('failures', 0)} | "
        f"{totals.get('retries', 0)} | {totals.get('total_ms', 0)} | "
        f"{totals.get('total_tokens', 0)} |"
    )
    return "\n".join(lines)


def _budget_table(state: Any, trace_summary: dict[str, Any] | None) -> str:
    """成本与预算：花了多少、还剩多少、有没有因为预算降级。"""

    settings = get_settings()
    budget = state.get("budget") or {}
    totals = (trace_summary or {}).get("totals") or {}
    used_tokens = int(budget.get("used_tokens") or totals.get("total_tokens") or 0)
    max_run_tokens = int(budget.get("max_run_tokens") or settings.budget.max_run_tokens or 0)
    elapsed = float(budget.get("elapsed_seconds") or 0.0)
    max_seconds = float(budget.get("max_run_seconds") or settings.budget.max_run_seconds or 0.0)

    lines = ["成本与预算：", ""]
    lines.append(
        f"- Token：已用 {used_tokens}"
        + (f" / {max_run_tokens}" if max_run_tokens else "（未设上限）")
    )
    lines.append(
        f"- 时间：{elapsed:.0f}s" + (f" / {max_seconds:.0f}s" if max_seconds else "（未设上限）")
    )
    calls = budget.get("calls_per_agent") or {}
    if calls:
        lines.append("- 各 Agent 调用次数：" + "，".join(f"{k} {v}" for k, v in calls.items()))
    lines.append(
        f"- 重试 {budget.get('retries', totals.get('retries', 0))} 次，"
        f"失败 {budget.get('failures', totals.get('failures', 0))} 次"
    )
    if (state.get("audit_meta") or {}).get("budget_stopped"):
        lines.append("- 降级：Audit 因预算不足提前停止（只审查了部分内容）")
    if budget.get("exceeded"):
        lines.append(f"- 已超出预算并降级：{budget.get('reason')}")
    return "\n".join(lines)


def write_outputs(
    state: Any,
    *,
    trace_summary: dict[str, Any] | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """把报告、修改后论文、状态快照写入 data/runs/<run_id>/。"""

    log = logger or print
    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    run_dir = settings.paths.run_dir(run_id)

    # 先把改动写回 Word（另存副本）：这样报告里能一起说明结果
    redline = write_redline_docx(state, run_dir=run_dir, logger=log)
    if redline:
        state = {**state, **redline}

    report = build_report(state, trace_summary=trace_summary)

    paths: dict[str, str] = {}
    try:
        run_dir.mkdir(parents=True, exist_ok=True)

        report_path = run_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        paths["report_path"] = str(report_path)

        document_path = run_dir / "revised_document.md"
        document_path.write_text(state.get("current_document") or "", encoding="utf-8")
        paths["document_path"] = str(document_path)

        original_path = run_dir / "original_document.md"
        original_path.write_text(state.get("original_document") or "", encoding="utf-8")
        paths["original_path"] = str(original_path)

        if redline:
            paths["docx_path"] = str(redline.get("docx_path") or "")

        edits_path = run_dir / "edits.json"
        edits_path.write_text(
            json.dumps(
                {
                    "history": state.get("revision_history") or [],
                    "applied_all_rounds": all_applied_edits(state),
                    "failed_all_rounds": all_failed_edits(state),
                    "latest_round_applied": state.get("revision_edits") or [],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        paths["edits_path"] = str(edits_path)

        # 状态快照：剔除体积很大的 trace（trace 已单独写在 trace.jsonl）
        snapshot = {key: value for key, value in dict(state).items() if key != "trace"}
        state_path = run_dir / "state.json"
        state_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        paths["state_path"] = str(state_path)

        if trace_summary:
            trace_path = run_dir / "trace_summary.json"
            trace_path.write_text(
                json.dumps(trace_summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            paths["trace_path"] = str(trace_path)
    except OSError as exc:
        log(f"[Report] 写入产物失败：{exc}")
        return {"final_report": report, "errors": [f"写入产物失败：{exc}"]}

    log(f"[Report] 产物已写入：{run_dir}")
    return {"final_report": report, **redline, **paths}


def load_state_snapshot(path: str | Path) -> dict[str, Any]:
    """读取之前保存的状态快照（用于从快照继续任务）。"""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def current_trace_summary(run_id: str) -> dict[str, Any]:
    """获取某个 run 的 Trace 汇总。"""

    return get_recorder(run_id).summary()
