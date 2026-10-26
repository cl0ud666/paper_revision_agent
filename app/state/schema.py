"""
定义整个论文修改 Workflow 使用的 State。

State 可以理解为：
**“当前这个论文修改任务，此刻已经知道的所有重要信息”。**

要点：

1. State ≠ Context。
   State 是任务状态（完整、结构化），Context 是某个 Agent 调用 LLM 时
   真正需要看到的那一小部分。两者之间的裁剪逻辑在 ``app.utils.context``。
2. 累积型字段（trace / history）使用 ``Annotated[list, add]``，
   LangGraph 会做“追加”而不是“覆盖”。
3. 所有字段必须可序列化（str / int / float / list / dict / None），
   因为要经过 Checkpointer 持久化，并支持 Human-in-the-loop 中断后恢复。
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict

# Orchestrator 允许输出的下一步动作（与 Router 的映射保持一致）
ROUTE_ACTIONS = ("audit", "revision", "human_approval", "report")

# 问题严重程度（用于排序）
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# 问题分类（Audit 只能从这些类别里选）
ISSUE_CATEGORIES = (
    "语言表达",
    "术语使用",
    "逻辑结构",
    "方法描述",
    "实验分析",
    "结论",
    "学术规范",
    "降重",
    # 专有名词（方法名/模型名/数据集名/人名/缩写）的写法与大小写
    "专有名词",
)


class Issue(TypedDict, total=False):
    """Audit / Review 输出的结构化问题。"""

    id: int
    # fix（确实写错，自动改）/ decision（前后冲突，要作者拍板）/
    # advice（没写错但可以更好、以及所有代码问题，只写批注）
    kind: str
    # kind=decision 时给出的可选项（2~3 条），让作者直接挑一个
    options: list[str]
    # 同一条问题涉及的**全部位置**。
    # "不重复报"指的是同一个问题只写一遍，**不是**标号只能有一个——
    # 一个术语在多处写法不一致，就是一条问题带多个标号。
    span_labels: list[str]
    category: str
    severity: str
    location: str
    problem: str
    evidence: str
    suggestion: str
    chunk_index: int
    source: str
    approved: bool


class ReviewResult(TypedDict, total=False):
    """Review Agent 的独立检查结果。"""

    status: str  # "pass" / "fail"
    summary: str
    per_issue: list[dict[str, Any]]
    remaining_issues: list[Issue]
    constraint_violations: list[str]
    dimension_scores: dict[str, Any]
    confidence: float
    checks: list[dict[str, Any]]
    error: str


class TraceEvent(TypedDict, total=False):
    """轻量 Trace：记录每个 Agent 的执行情况。"""

    agent: str
    action: str
    iteration: int
    duration_ms: float
    attempts: int
    ok: bool
    error: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    extra: dict[str, Any]


class PaperState(TypedDict, total=False):
    """
    论文修改任务的全局状态。

    每个 Workflow Node 都可以读取 State，并返回“需要更新的字段”。
    """

    # ---------- 任务输入 ----------
    run_id: str
    user_request: str
    original_document: str
    current_document: str
    # 原始 .docx 的路径：改完要写回它（另存副本），所以必须留住"原文件"
    source_docx: str
    visual_review: bool
    final_review_ready: bool
    final_review_confirmed: bool
    final_patches: list[dict[str, Any]]
    preview_manifest: dict[str, Any]
    unfinished_items: list[dict[str, Any]]
    review_diagnostics: list[dict[str, Any]]
    deferred_advice: list[dict[str, Any]]
    selected_decisions: list[dict[str, Any]]
    manual_items: list[dict[str, Any]]
    final_manual_items: list[dict[str, Any]]
    input_warnings: list[str]

    # ---------- Orchestrator ----------
    plan_kind: str  # full_review / direct_revision / replan
    next_action: str  # audit / revision / human_approval / report
    plan: list[str]
    plan_reason: str
    focus_areas: list[str]
    next_instructions: str

    # ---------- Audit ----------
    audit_findings: list[Issue]
    audit_meta: dict[str, Any]
    audit_failed: bool
    # 问题编号计数器（多轮时不要重复用同一个编号）
    issue_counter: int

    # ---------- 文档模型与补丁 ----------
    # 文档版本号：每次应用补丁 +1（用来串起新旧 span ID）
    document_version: int
    # 当前文档的 span 索引（精简：不含全文）
    spans: list[dict[str, Any]]
    # 本轮提出的补丁（一次性全部产出，用户在清单上一次性勾选）
    proposed_patches: list[dict[str, Any]]
    # 补丁编号计数器
    patch_counter: int
    # 用户对补丁的决定：{"patch-001": {"decision": "accept", "note": ""}}
    patch_decisions: dict[str, dict[str, Any]]
    patch_stats: dict[str, Any]
    # 已应用 / 已被拒绝的补丁（累积，跨轮可回溯）
    applied_patches: Annotated[list[dict[str, Any]], add]
    rejected_patches: Annotated[list[dict[str, Any]], add]
    # 无法定位（冲突）的补丁（本轮）
    conflict_patches: list[dict[str, Any]]
    # 历史被拒补丁的精简记忆（避免下一轮重复提同一个改动）
    rejected_memory: list[dict[str, Any]]
    # 是否启用“先确认要改哪些问题”的闸门（默认关闭）
    issue_gate: bool

    # ---------- Human-in-the-loop ----------
    approved_issue_ids: list[int]
    # 用户要求与决定的统一存储（带编号、可撤改、跨轮持久）
    # 例如：{"id": "R1", "kind": "rule", "text": "全文统一把「土豆」写成「马铃薯」", ...}
    user_rules: list[dict[str, Any]]
    # 用户是否已经对"前后矛盾"拍过板。
    # 必须在这里声明：LangGraph 只保留 State 里声明过的字段，
    # 没声明的键会被**静默丢弃**（曾经因此让决策闸门反复触发）。
    decisions: bool
    # 用户在"写入 Word 前的最后一次确认"里剔除掉的补丁编号（不写进 Word）
    excluded_patch_ids: list[str]
    # 兼容字段：active 要求的纯文本列表（旧代码 / 报告仍在读）
    human_constraints: list[str]
    human_feedback: str
    human_decision: str  # approve / reject
    human_rounds: Annotated[list[dict[str, Any]], add]

    # ---------- Revision ----------
    revision_edits: list[dict[str, Any]]
    revision_failed_edits: list[dict[str, Any]]
    revision_summary: str
    revision_history: Annotated[list[dict[str, Any]], add]

    # ---------- Review ----------
    review_result: ReviewResult | None
    review_history: Annotated[list[dict[str, Any]], add]

    # ---------- 控制与观测 ----------
    iteration: int
    max_iterations: int
    errors: Annotated[list[str], add]
    trace: Annotated[list[TraceEvent], add]

    # ---------- 多轮会话 ----------
    turn: int
    turn_started_at: str
    # 每轮的输入与结果（由 follow_up 整体覆盖，不使用累加器）
    turns: list[dict[str, Any]]
    conversation: list[dict[str, Any]]

    # ---------- 预算 ----------
    run_started_at: str
    budget: dict[str, Any]

    # ---------- 输出 ----------
    final_report: str
    report_path: str
    document_path: str
    # 带 Word 修订痕迹与批注的 .docx 副本
    docx_path: str
    redline_summary: dict[str, Any]


def initial_state(
    user_request: str,
    document: str,
    *,
    run_id: str = "adhoc",
    max_iterations: int = 3,
) -> PaperState:
    """构造一个可运行的初始 State。"""

    return PaperState(
        run_id=run_id,
        user_request=user_request,
        original_document=document,
        current_document=document,
        source_docx="",
        document_version=1,
        spans=[],
        proposed_patches=[],
        patch_counter=0,
        patch_decisions={},
        patch_stats={},
        applied_patches=[],
        rejected_patches=[],
        conflict_patches=[],
        rejected_memory=[],
        issue_gate=False,
        plan_kind="",
        next_action="",
        plan=[],
        plan_reason="",
        focus_areas=[],
        next_instructions="",
        audit_findings=[],
        audit_meta={},
        audit_failed=False,
        issue_counter=0,
        approved_issue_ids=[],
        user_rules=[],
        human_constraints=[],
        human_feedback="",
        human_decision="",
        revision_edits=[],
        revision_failed_edits=[],
        revision_summary="",
        review_result=None,
        iteration=0,
        max_iterations=max_iterations,
        turn=1,
        turn_started_at="",
        turns=[],
        conversation=[],
        run_started_at="",
        budget={},
        errors=[],
        trace=[],
        final_report="",
        report_path="",
        document_path="",
        docx_path="",
        redline_summary={},
    )
