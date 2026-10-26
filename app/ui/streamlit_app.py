"""
Streamlit 界面：把 Multi-Agent Workflow 和人机协作过程可视化。

启动：

    streamlit run app/ui/streamlit_app.py

界面流程：

    上传/粘贴论文 → 开始修改 → Audit 给出问题清单 → 用户勾选问题 + 填写约束
    → Revision 修改 → Review 复核 → 查看报告 / 对照 / 运行观测

说明：Streamlit 的脚本会反复重跑，因此 Harness（以及它内部的 Graph 与
Checkpointer）必须保存在 st.session_state 里，才能在中途停下等人时保住状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从项目根目录直接运行本文件
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.utils.llm import describe_configuration  # noqa: E402
from app.utils.loaders import DocumentLoadError, load_bytes, supported_extensions  # noqa: E402
from app.workflow.harness import Harness  # noqa: E402
from app.workflow.interaction import dispatch_request  # noqa: E402

SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


def keep_source_docx(data: bytes, name: str) -> str:
    """
    把上传的 .docx 存到磁盘，返回路径。

    为什么要落盘：改完要把它当"原文件"写回一份带修订痕迹的副本，
    而任务开始到出结果之间会隔着好几次人工确认，内存里留不住。
    按内容哈希命名，同一份文件重复上传不会堆一堆副本。
    """

    import hashlib

    folder = get_settings().paths.data_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:12]
    safe_name = Path(name).name or "paper.docx"
    path = folder / f"{digest}-{safe_name}"
    if not path.exists():
        path.write_bytes(data)
    return str(path)


def collect_edits(state: dict) -> list[dict]:
    """汇总所有轮次真正落实的修改（revision_edits 只有最后一轮）。"""

    collected: list[dict] = []
    for entry in state.get("revision_history") or []:
        for edit in entry.get("applied") or []:
            item = dict(edit)
            item.setdefault("round", entry.get("iteration"))
            collected.append(item)
    return collected or list(state.get("revision_edits") or [])


def collect_failed_edits(state: dict) -> list[dict]:
    """汇总所有轮次未能落实的修改。"""

    collected: list[dict] = []
    for entry in state.get("revision_history") or []:
        for edit in entry.get("failed_edits") or []:
            item = dict(edit)
            item.setdefault("round", entry.get("iteration"))
            collected.append(item)
    return collected or list(state.get("revision_failed_edits") or [])


def init_state() -> None:
    """初始化 session_state。"""

    st.session_state.setdefault("harness", None)
    st.session_state.setdefault("result", None)
    st.session_state.setdefault("document", "")


def sidebar(settings) -> dict:
    """左侧参数区。"""

    st.sidebar.header("运行参数")
    config = describe_configuration()
    st.sidebar.caption(f"模型：{config['model']}｜API Key：{config['api_key'] or '未配置'}")

    max_iterations = st.sidebar.number_input(
        "最大修改轮次",
        min_value=1,
        max_value=6,
        value=settings.workflow.max_iterations,
        help="Review 未通过时会循环修改，超过这个轮次就输出当前最优结果。",
    )
    auto_approve = st.sidebar.checkbox(
        "自动批准全部问题（跳过人工确认）",
        value=False,
        help="用于快速跑通流程；正式使用时建议手动确认修改范围。",
    )
    constraints_text = st.sidebar.text_area(
        "修改约束（每行一条）",
        value="不要修改实验结果与数据\n保持作者原有表达风格",
        height=100,
        help="这些约束会进入 Revision 与 Review 的判断依据。",
    )
    return {
        "max_iterations": int(max_iterations),
        "auto_approve": auto_approve,
        "constraints": [line.strip() for line in constraints_text.splitlines() if line.strip()],
    }


def document_input() -> tuple[str, str]:
    """论文输入区，返回 (用户任务, 论文文本)。"""

    st.subheader("1 准备论文")
    request = st.text_input(
        "你的任务",
        value="请全面检查并修改这篇论文。",
        help="例如：只修改摘要，不要审查全文。",
    )

    tab_upload, tab_paste, tab_sample = st.tabs(["上传文件", "粘贴文本", "使用示例"])

    with tab_upload:
        uploaded = st.file_uploader(
            f"选择论文文件（{supported_extensions()}）",
            type=["docx", "pdf", "md", "markdown", "txt"],
        )
        if uploaded is not None:
            try:
                loaded = load_bytes(uploaded.getvalue(), filename=uploaded.name)
            except DocumentLoadError as exc:
                st.error(f"读取失败：{exc}")
            else:
                st.session_state["document"] = loaded.text
                # .docx 才留下原文件：改完要写回它做一份带修订痕迹的副本
                st.session_state["source_docx"] = (
                    keep_source_docx(uploaded.getvalue(), uploaded.name)
                    if uploaded.name.lower().endswith(".docx")
                    else ""
                )
                st.success(f"已读取：{uploaded.name}（{loaded.describe()}）")
                from app.document.spans import ZONE_NAMES, build_spans, structure_report

                report = structure_report(build_spans(loaded.text))
                st.caption(
                    "结构自检："
                    + "，".join(
                        f"{ZONE_NAMES.get(zone, zone)} {count}"
                        for zone, count in sorted(
                            report["zones"].items(), key=lambda item: -item[1]
                        )
                    )
                    + f"；默认送审 {report['auditable']} 块"
                )

    with tab_paste:
        pasted = st.text_area("粘贴论文内容", height=200)
        if st.button("使用粘贴内容") and pasted.strip():
            st.session_state["document"] = pasted
            st.session_state["source_docx"] = ""
            st.success("已载入粘贴内容。")

    with tab_sample:
        sample_path = get_settings().paths.samples_dir / "sample_paper.md"
        if sample_path.exists() and st.button("载入示例论文"):
            st.session_state["document"] = sample_path.read_text(encoding="utf-8")
            st.session_state["source_docx"] = ""
            st.success("已载入示例论文。")

    return request, st.session_state.get("document", "")


def show_issues(request: dict) -> dict | None:
    """
    人机协作界面：展示 Audit 的问题清单，收集用户确认与约束。

    返回 resume 用的 payload；用户尚未确认时返回 None。
    """

    issues = request.get("issues") or []
    st.subheader("2 确认修改范围")
    st.caption(request.get("instruction", ""))

    if not issues:
        return None

    options = {issue["id"]: issue for issue in issues}
    default_ids = [issue["id"] for issue in issues]

    selected = st.multiselect(
        "选择需要修改的问题（默认全选）",
        options=list(options.keys()),
        default=default_ids,
        format_func=lambda issue_id: (
            f"[{issue_id}] {SEVERITY_LABEL.get(options[issue_id].get('severity', ''), '')}"
            f" {options[issue_id].get('category', '')}｜{options[issue_id].get('problem', '')}"
        ),
    )

    with st.expander("查看问题详情", expanded=True):
        for issue in issues:
            st.markdown(
                f"**[{issue['id']}] {issue.get('category')}"
                f"（{SEVERITY_LABEL.get(issue.get('severity', ''), '')}）** "
                f"{issue.get('problem')}"
            )
            if issue.get("location"):
                st.caption(f"位置：{issue['location']}｜建议：{issue.get('suggestion') or '—'}")

    constraints_text = st.text_area(
        "这一轮不希望被改动的内容 / 额外要求（每行一条）",
        value="\n".join(st.session_state.get("pending_constraints", [])),
        height=100,
    )
    feedback = st.text_input("补充说明（可选）", value="")

    col_confirm, col_reject = st.columns(2)
    constraints = [line.strip() for line in constraints_text.splitlines() if line.strip()]

    if col_confirm.button("确认并开始修改", type="primary"):
        return {
            "decision": "approve",
            "approved_issue_ids": selected,
            "constraints": constraints,
            "feedback": feedback,
            "iteration": request.get("iteration"),
        }

    if col_reject.button("都不修改，直接出报告"):
        return {
            "decision": "reject",
            "approved_issue_ids": [],
            "constraints": constraints,
            "feedback": feedback,
            "iteration": request.get("iteration"),
        }

    return None


def show_issue_decisions(request: dict) -> dict | None:
    st.subheader("2 选择统一口径")
    st.caption(request.get("instruction", ""))
    choices = {}
    prefix = f"{request.get('run_id')}:{request.get('turn')}:{request.get('iteration')}"
    for issue in request.get("issues") or []:
        st.markdown(f"**{issue.get('problem', '')}**")
        options = issue.get("options") or []
        selection = st.radio(
            "请选择处理方式",
            options=[-1, *range(len(options))],
            format_func=lambda i, options=options: "先不修改" if i == -1 else options[i],
            key=f"{prefix}:choice:{issue['id']}",
        )
        if selection != -1:
            choices[str(issue["id"])] = selection
    rules_text = st.text_area("补充判断 / 额外要求（每行一条）", key=f"{prefix}:rules")
    if st.button("确认选择并继续", key=f"{prefix}:submit", type="primary"):
        return {
            "choices": choices,
            "rules": [line.strip() for line in rules_text.splitlines() if line.strip()],
        }
    return None


def _show_patch_details(patches: list[dict]) -> None:
    for patch in patches:
        st.markdown(
            f"**[{patch.get('patch_id')}] {patch.get('position') or patch.get('span_label') or '全文'}**"
        )
        if patch.get("problem"):
            st.caption(patch["problem"])
        st.write("原文：", patch.get("before") or patch.get("quote") or "（插入）")
        st.write("修改为：", patch.get("replace") or "（删除）")
        st.caption(f"原因：{patch.get('rationale') or '—'}")
        if patch.get("count"):
            st.caption(f"共 {patch['count']} 处")
        if patch.get("conflict_reason"):
            st.warning(patch["conflict_reason"])


def show_patch_review(request: dict) -> dict | None:
    st.subheader("2 审阅修改清单")
    st.caption(request.get("instruction", ""))
    patches = request.get("patches") or []
    _show_patch_details(patches)
    by_id = {p["patch_id"]: p for p in patches if p.get("status") == "proposed"}
    selected = st.multiselect(
        "接受哪些修改",
        options=list(by_id),
        default=list(by_id),
        key=f"{request.get('run_id')}:{request.get('iteration')}:patches",
    )
    if st.button("确认修改并继续", type="primary"):
        return {"accepted_ids": selected}
    return None


def show_final_confirm(request: dict) -> dict | None:
    st.subheader("2 确认生成文件")
    st.caption(request.get("instruction", ""))
    patches = request.get("patches") or []
    _show_patch_details(patches)
    previous = set(request.get("defaults", {}).get("excluded_ids") or [])
    options = [p["patch_id"] for p in patches]
    default = [
        p["patch_id"]
        for p in patches
        if set(p.get("patch_ids") or [p["patch_id"]]).issubset(previous)
    ]
    excluded = st.multiselect(
        "剔除哪些修改（不选则全部写入）",
        options=options,
        default=default,
        key=f"{request.get('run_id')}:{request.get('turn')}:excluded",
    )
    if st.button("确认并生成文件", type="primary"):
        return {"excluded_ids": excluded}
    if st.button("全部不写入"):
        return {"exclude_all": True}
    return None


def show_request(request: dict) -> dict | None:
    return dispatch_request(
        request,
        {
            "issue_review": show_issue_decisions,
            "patch_review": show_patch_review,
            "final_confirm": show_final_confirm,
            "human_approval": show_issues,
        },
    )


def launch_run(harness: Harness, options: dict, request: str, document: str, source_docx: str):
    run = harness.run_to_completion if options["auto_approve"] else harness.start
    return run(request, document, max_iterations=options["max_iterations"], source_docx=source_docx)


def show_results(result) -> None:
    """展示最终结果。"""

    state = result.state
    st.subheader("3 修改结果")

    columns = st.columns(4)
    columns[0].metric("Review 结论", (result.review_status or "未执行").upper())
    columns[1].metric("修改轮次", state.get("iteration", 0))
    columns[2].metric("落实修改", len(state.get("revision_edits") or []))
    columns[3].metric(
        "剩余问题", len((state.get("review_result") or {}).get("remaining_issues") or [])
    )

    tab_report, tab_document, tab_diff, tab_trace = st.tabs(
        ["报告", "修改后论文", "修改对照", "运行观测"]
    )

    with tab_report:
        st.markdown(result.final_report or "（无报告）")
        if result.report_path:
            st.download_button(
                "下载报告",
                data=(result.final_report or "").encode("utf-8"),
                file_name="report.md",
                mime="text/markdown",
            )

    with tab_document:
        if result.docx_path and Path(result.docx_path).exists():
            st.success(
                "已生成 Word 副本（保留原文件，改动以**修订痕迹**标注，"
                "每条改动都带批注说明理由）。在 Word / WPS 里打开后，"
                "可以逐条接受或拒绝。"
            )
            docx_bytes = Path(result.docx_path).read_bytes()
            st.download_button(
                "下载 Word 副本（带修订痕迹）",
                data=docx_bytes,
                file_name=Path(result.docx_path).name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                type="primary",
            )
            skipped = (state.get("redline_summary") or {}).get("skipped")
            if skipped:
                st.warning(
                    f"另有 {skipped} 条改动无法自动写回 Word"
                    "（多与公式、图片或跨单元格有关），已在文档原位留批注。"
                )
                with st.expander("看看是哪几条、该怎么手工改", expanded=True):
                    for item in (state.get("redline_summary") or {}).get("skipped_items") or []:
                        position = item.get("position") or item.get("location") or ""
                        location = item.get("location") or ""
                        if location and location not in str(position):
                            position = f"{position}（{location}）" if position else location
                        st.markdown(
                            f"**[{item.get('patch_id')}] {position}**\n\n"
                            f"- 原文：{item.get('before') or '—'}\n"
                            f"- 建议改为：{item.get('after') or '（删除）'}\n"
                            f"- 没自动改的原因：{item.get('reason') or '—'}"
                        )
            st.divider()

        st.text_area("修改后论文", result.revised_document, height=400)
        st.download_button(
            "下载文字稿（Markdown）",
            data=(result.revised_document or "").encode("utf-8"),
            file_name="revised_document.md",
            mime="text/markdown",
        )

    with tab_diff:
        edits = collect_edits(state)
        if not edits:
            st.info("本次没有产生修改。")
        for edit in edits:
            st.markdown(
                f"**第 {edit.get('round', '-')} 轮｜问题 {edit.get('issue_id')}**"
                f"（{edit.get('rationale') or '—'}）"
            )
            col_before, col_after = st.columns(2)
            col_before.markdown("修改前")
            col_before.info(edit.get("original", ""))
            col_after.markdown("修改后")
            col_after.success(edit.get("revised", ""))

        failed = collect_failed_edits(state)
        if failed:
            st.warning(f"{len(failed)} 处修改未能落实：")
            for edit in failed:
                st.caption(f"- 问题 {edit.get('issue_id')}：{edit.get('reason')}")

    with tab_trace:
        trace = state.get("trace") or []
        st.caption("流程事件：")
        st.json(trace)
        if st.session_state.get("harness") is not None:
            st.caption("统计汇总：")
            st.json(st.session_state["harness"].trace_summary())

    if result.status == "failed":
        st.error(f"运行失败：{result.error}")


def main() -> None:
    st.set_page_config(page_title="学术论文智能修改系统", layout="wide")
    init_state()

    settings = get_settings()
    st.title("学术论文智能修改 Multi-Agent 系统")
    st.caption(
        "LangGraph Workflow：Orchestrator → Audit → Human Approval → Revision → Review → Report"
    )

    options = sidebar(settings)
    request, document = document_input()

    if st.button("开始修改", type="primary", disabled=not document.strip()):
        harness = Harness(
            auto_approve=options["auto_approve"],
            auto_approve_constraints=options["constraints"],
            logger=lambda message: None,
        )
        st.session_state["harness"] = harness
        with st.spinner("正在理解任务并规划执行路径..."):
            st.session_state["result"] = launch_run(
                harness, options, request, document, st.session_state.get("source_docx", "")
            )
        st.rerun()

    result = st.session_state.get("result")
    harness = st.session_state.get("harness")
    if result is None or harness is None:
        st.info("请先准备论文，然后点击“开始修改”。")
        return

    if result.status == "failed":
        st.error(f"运行失败：{result.error}")
        return

    if result.is_interrupted:
        payload = show_request(result.pending_request)
        if payload is not None:
            if "constraints" in payload:
                st.session_state["pending_constraints"] = payload["constraints"]
            with st.spinner("正在修改论文并做独立复核..."):
                st.session_state["result"] = harness.resume(payload)
            st.rerun()
    else:
        show_results(result)


if __name__ == "__main__":
    main()
