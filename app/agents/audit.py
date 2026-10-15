"""
Audit Agent：系统性发现问题。

流程：

    论文 → 切成可寻址的块（span） → 按块分组
         → ① 并发逐块审查（ThreadPoolExecutor）
         → ② 跨批对齐（把全部分块的发现摆到一起，合并重复 / 指出互相矛盾）
         → ③ 全文矛盾核查（不分块，整篇送审，专查前后打架）
         → ④ 逐字核对证据 + 合并去重 + 按严重程度取舍

与上一版的区别：**分块按段落边界切**（不是按字符切），
因此模型看到的每个块都带段落 ID（``[P03]``），
它给出的每条问题也都会带上这个 ID——后续 Revision 才能精确落补丁。

为什么是"并发 + 事后全局对齐"，而不是"后面的批次看前面的发现"：
分块之间没有依赖（每块只读自己的文本），所以可以并行；但也正因为并行，
任何一块都看不到别的块报了什么。串行的"后者看前者"只有后面的能看到前面的，
全局对齐则让每个发现都要和其它所有发现对一遍，覆盖更全。

硬约束：evidence 必须逐字来自该段落，这样补丁校验能一次通过。
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from app.agents.prompts import (
    AUDIT_SYSTEM,
    CROSS_CHECK_SYSTEM,
    NAME_CHECK_SYSTEM,
    TARGETED_CHECK_SYSTEM,
    WHOLE_PAPER_SYSTEM,
    render_audit_prompt,
    render_cross_check_prompt,
    render_name_check_prompt,
    render_targeted_check_prompt,
    render_whole_paper_prompt,
)
from app.config import Settings, get_settings
from app.document.spans import (
    Span,
    build_spans,
    chunk_spans,
    index_summary,
    render_annotated,
    resolve_span,
    select_auditable,
    structure_report,
)
from app.state.schema import ISSUE_CATEGORIES, SEVERITY_ORDER
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.consistency import (
    collect_proper_names,
    describe_entity_check,
    describe_naming_variants,
    find_cross_section_gaps,
    find_naming_variants,
    find_near_miss_names,
)
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.rules import is_ignored
from app.utils.text import (
    estimate_tokens,
    locate_section,
    looks_like_code,
    normalize_whitespace,
    similarity,
)

SEVERITIES = ("high", "medium", "low")
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _consistency_hint(
    chunk_labels: set[str],
    *,
    naming: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
) -> str:
    """
    只挑"与本批次有关"的一致性线索放进上下文。

    全量塞进去有两个坏处：占掉审查正文的预算；同一个词会被每批评一次。
    所以三类线索都按"是否出现在本批次"过滤。
    """

    def relevant(labels: list[str]) -> bool:
        return any(label in chunk_labels for label in labels)

    relevant_naming = [
        item
        for item in naming
        if relevant([label for labels in (item.get("variants") or {}).values() for label in labels])
    ]
    relevant_gaps = [item for item in gaps if relevant(list(item.get("labels") or []))]
    relevant_misses = [
        item
        for item in near_misses
        if relevant(list(item.get("suspected_labels") or []))
        or relevant(list(item.get("likely_labels") or []))
    ]

    sections: list[str] = []
    naming_text = describe_naming_variants(relevant_naming)
    if naming_text:
        sections.append(naming_text)
    sections.extend(describe_entity_check(relevant_gaps, relevant_misses))
    return "\n\n".join(sections)


# ---------------------------------------------------------------- 专名核验

# 一次核验最多看多少个专名（清单很短，几十个词足够覆盖一篇论文）
MAX_NAME_CANDIDATES = 60


def _name_context_snippet(text: str, form: str, *, width: int = 26) -> str:
    """取专名附近的一小段原文，给模型判断"这是不是那个产品/方法"。"""

    index = text.find(form)
    if index < 0:
        return ""
    start = max(0, index - width)
    end = min(len(text), index + len(form) + width)
    # 压成一行：清单是一条一行，原文里夹换行会把列表撑乱
    return normalize_whitespace(text[start:end])


def _name_candidates(
    spans: list[Span],
    *,
    exclude: set[str] | None = None,
    limit: int = MAX_NAME_CANDIDATES,
) -> list[dict[str, Any]]:
    """
    挑出要核验的专名清单。

    排序刻意"罕见优先"：真正的错字通常只出现一两次，
    而出现几十次的词基本是作者反复在用的正确写法。
    """

    blocked = exclude or set()
    stats = collect_proper_names(spans)
    by_label = {span.label: span for span in spans}

    candidates: list[dict[str, Any]] = []
    for form, stat in stats.items():
        if form in blocked:
            continue
        snippet = ""
        for label in stat.labels:
            span = by_label.get(label)
            if span is not None:
                snippet = _name_context_snippet(span.text, form)
                if snippet:
                    break
        candidates.append(
            {
                "name": form,
                "count": stat.count,
                "labels": list(stat.labels)[:4],
                "snippet": snippet,
            }
        )
    candidates.sort(key=lambda item: (int(item["count"]), str(item["name"])))
    return candidates[:limit]


def _render_name_list(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for item in candidates:
        where = "、".join(item.get("labels") or []) or "未定位"
        snippet = item.get("snippet") or ""
        tail = f"　原文：…{snippet}…" if snippet else ""
        lines.append(f"- {item['name']}（出现 {item['count']} 次，见 {where}）{tail}")
    return "\n".join(lines)


def _name_issue(
    spans: list[Span],
    *,
    form: str,
    correct: str,
    reason: str,
    source: str,
) -> dict[str, Any] | None:
    """
    把"某个专名写法不对"变成一条可核对的问题。

    evidence 直接从原文里取一段（保证逐字可核对），span 取第一次出现的位置。
    """

    for span in spans:
        if form not in span.text:
            continue
        evidence = _name_context_snippet(span.text, form, width=24)
        if len(evidence) < MIN_EVIDENCE_CHARS:
            evidence = span.text.strip()[:120]
        if len(evidence) < MIN_EVIDENCE_CHARS:
            return None
        return {
            "category": "专有名词",
            "severity": "medium",
            "location": span.section or span.label,
            "problem": f"「{form}」写法有误：应为「{correct}」。（{reason}）",
            "evidence": evidence,
            "suggestion": f"把「{form}」改成「{correct}」，并检查全文其它位置是否同错",
            "span_id": span.id,
            "span_label": span.label,
            "source": source,
        }
    return None


def _name_check_issues(
    state: Any,
    *,
    spans: list[Span],
    log: Callable[[str], None],
    recorder: Any | None,
    iteration: int,
    run_id: str,
) -> list[dict[str, Any]]:
    """
    专名核验：**单独一次调用**判断"写法本身对不对"。

    两类来源：

    1. 确定性：文中已经出现对照（P04 写 ESC、P03 写 ECS）——不需要模型；
    2. 世界知识：阿里云的产品就叫 ECS，**正确写法根本不需要出现在文中**，
       所以只能靠模型对领域/产品的既有知识来判断——
       这正是"再看前后文"抓不到的那一类。

    为什么单独一次调用而不是塞进逐段审查：混在"审论证、审表达"的任务里，
    模型不会去核每个专名的标准写法（这就是同一篇论文跑三次只中一次的原因）。
    清单很短，单独核一遍很便宜，而且**每次都核到全部专名**。
    """

    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) 文中已有对照：确定性，不花模型调用
    for item in find_near_miss_names(spans):
        suspected = str(item.get("suspected") or "")
        likely = str(item.get("likely") or "")
        if not suspected or not likely:
            continue
        issue = _name_issue(
            spans,
            form=suspected,
            correct=likely,
            reason=f"文中多处写作「{likely}」，两者只差一个字符，疑似笔误",
            source="name_check",
        )
        if issue is not None:
            seen.add(suspected)
            issues.append(issue)

    # 2) 世界知识核验
    candidates = _name_candidates(spans, exclude=seen)
    if not candidates:
        return issues

    fields = build_context(
        state,
        "audit",
        extra={"name_list": _render_name_list(candidates)},
    )
    prompt = render_name_check_prompt(format_context(fields))
    guard = get_guard("audit", on_event=log)
    try:
        response = guard.run(
            partial(
                call_llm,
                prompt,
                "audit",
                system=NAME_CHECK_SYSTEM,
                json_mode=True,
                run_id=run_id,
                action="name_check",
            ),
            action="name_check",
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            extra={"candidates": len(candidates)},
        )
        data = parse_json_object(response.text, required_keys=(), context="TermCheck")
    except Exception as exc:  # noqa: BLE001 - 核验失败只保留确定性结果
        log(f"[Audit] 专名核验失败（{type(exc).__name__}: {exc}），只保留词表比对结果。")
        return issues

    known = {item["name"] for item in candidates}
    for item in data.get("suspected") or []:
        if not isinstance(item, dict):
            continue
        form = str(item.get("name") or "").strip()
        correct = str(item.get("correct") or "").strip()
        reason = str(item.get("reason") or "").strip() or "写法不符合该名称的标准形式"
        confidence = str(item.get("confidence") or "medium").strip().lower()
        if not form or not correct or form == correct:
            continue
        if form not in known:
            continue  # 模型自己造的词，不接受（防幻觉）
        if confidence == "low":
            continue
        if form in seen:
            continue
        issue = _name_issue(
            spans, form=form, correct=correct, reason=reason, source="name_check"
        )
        if issue is not None:
            seen.add(form)
            issues.append(issue)

    log(f"[Audit] 专名核验：核了 {len(candidates)} 个专名，发现 {len(issues)} 处疑似写法问题。")
    return issues


# 证据必须能在段落里逐字找到，且不能太短（太短容易"碰巧命中"）
MIN_EVIDENCE_CHARS = 8

# 同类系统性问题聚合的相似度阈值
SYSTEMATIC_SIMILARITY = 0.6


def _serialized_logger(log: Callable[[str], None]) -> Callable[[str], None]:
    """
    把 logger 包成线程安全的。

    并发跑分块时多个线程会同时输出，不加锁的话行与行会互相穿插。
    这里只串行化**输出**，不串行化执行。
    """

    lock = threading.Lock()

    def emit(message: str) -> None:
        with lock:
            log(message)

    return emit


@dataclass
class _ChunkOutcome:
    """单个分块的审查结果。"""

    index: int
    issues: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False
    error: str = ""


@dataclass
class _ChunkBatch:
    """一轮分块审查的整体结果（按序号收集，供上层按顺序归并）。"""

    outcomes: dict[int, _ChunkOutcome] = field(default_factory=dict)
    budget_stopped: bool = False
    budget_snapshot: dict[str, Any] | None = None
    # 实际生效的并发度（受 PRA_AUDIT_CONCURRENCY 与分块总数共同约束）
    concurrency: int = 1


def _audit_one_chunk(
    index: int,
    chunk: list[Span],
    *,
    chunk_total: int,
    state: Any,
    guard: Any,
    emitter: Callable[[str], None],
    recorder: Any | None,
    run_id: str,
    iteration: int,
    document: str,
    naming_hint: list[dict[str, Any]],
    section_gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
) -> _ChunkOutcome:
    """
    审查单个分块（并发执行的最小单元）。

    这个函数**只读** state，不写任何共享状态：所有写回都由调用方在收集完
    结果后按分块顺序完成。分块之间没有依赖，因此可以安全并发。
    """

    chunk_text = render_annotated(chunk)
    chunk_labels = {span.label for span in chunk}
    consistency_hint = _consistency_hint(
        chunk_labels,
        naming=naming_hint,
        gaps=section_gaps,
        near_misses=near_misses,
    )
    fields = build_context(
        state,
        "audit",
        extra={
            "chunk_index": index,
            "chunk_total": chunk_total,
            "chunk_text": chunk_text,
            "chunk_labels": [span.label for span in chunk],
            "consistency_check": consistency_hint,
        },
    )
    prompt = render_audit_prompt(format_context(fields))

    try:
        response = guard.run(
            partial(
                call_llm,
                prompt,
                "audit",
                system=AUDIT_SYSTEM,
                json_mode=True,
                run_id=run_id,
                action=f"chunk_{index + 1}",
            ),
            action=f"chunk_{index + 1}",
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            extra={"chunk_index": index, "spans": len(chunk)},
        )
        data = parse_json_object(response.text, required_keys=("issues",), context="Audit")
    except Exception as exc:  # noqa: BLE001 - 单个分块失败不影响其它分块
        emitter(f"[Audit] 第 {index + 1} 批审查失败：{type(exc).__name__}: {exc}")
        return _ChunkOutcome(index=index, failed=True, error=str(exc))

    issues = [
        normalized
        for normalized in (
            _normalize_issue(item, chunk=chunk, document=document)
            for item in (data.get("issues") or [])
        )
        if normalized is not None
    ]
    return _ChunkOutcome(index=index, issues=issues)


def _run_chunks(
    chunks: list[list[Span]],
    *,
    state: Any,
    guard: Any,
    emitter: Callable[[str], None],
    recorder: Any | None,
    run_id: str,
    iteration: int,
    document: str,
    naming_hint: list[dict[str, Any]],
    section_gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
    settings: Settings,
) -> _ChunkBatch:
    """
    跑完所有分块的审查。

    - 并发度取 ``PRA_AUDIT_CONCURRENCY``，并受分块总数约束
      （不会为 3 个分块开 6 个线程）；
    - 预算检查放在**提交环节**（串行）：预算耗尽就不再提交剩余分块，
      保留"超预算降级而不是崩"的原有语义；
    - 单个分块失败只记一次失败，不影响其它分块；
    - 结果按分块序号收集，保证 issue 编号不随网络抖动变化。
    """

    batch = _ChunkBatch()
    if not chunks:
        return batch

    total = len(chunks)
    concurrency = max(1, int(settings.context.audit_concurrency or 1))
    concurrency = min(concurrency, total)
    batch.concurrency = concurrency

    def targets():
        for index, chunk in enumerate(chunks):
            try:
                check(state, agent="audit", stage=f"audit_chunk_{index + 1}")
            except BudgetExceeded as exc:
                batch.budget_stopped = True
                batch.budget_snapshot = exceeded_snapshot(state, exc)
                verb = "停止提交后续分块" if concurrency > 1 else "停止后续审查"
                emitter(f"[Audit] 预算不足，{verb}：{exc.reason}")
                return
            yield index, chunk

    common: dict[str, Any] = {
        "state": state,
        "guard": guard,
        "emitter": emitter,
        "recorder": recorder,
        "run_id": run_id,
        "iteration": iteration,
        "document": document,
        "naming_hint": naming_hint,
        "section_gaps": section_gaps,
        "near_misses": near_misses,
        "chunk_total": total,
    }

    if concurrency == 1:
        for index, chunk in targets():
            batch.outcomes[index] = _audit_one_chunk(index, chunk, **common)
        return batch

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="audit") as pool:
        futures: dict[Any, int] = {
            pool.submit(_audit_one_chunk, index, chunk, **common): index
            for index, chunk in targets()
        }
        if futures:
            emitter(
                f"[Audit] 并发审查 {len(futures)}/{total} 个分块（并发度 {concurrency}）。"
            )
        for future in as_completed(futures):
            index = futures[future]
            try:
                batch.outcomes[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 兜底：工作线程自身的异常
                batch.outcomes[index] = _ChunkOutcome(
                    index=index, failed=True, error=str(exc)
                )
                emitter(f"[Audit] 第 {index + 1} 批执行异常：{type(exc).__name__}: {exc}")
    return batch


def _apply_duplicate_groups(
    issues: list[dict[str, Any]],
    groups: Any,
) -> tuple[list[dict[str, Any]], int]:
    """
    按模型给出的编号分组合并重复的候选。

    只接受"编号"这种可验证的输出：模型**不重写**问题内容，只指出哪几条是同一件事，
    真正的合并由这里确定性地完成（保留组内编号最小的那条，取更严重的等级，补全缺失字段）。
    编造编号、越界编号、单条成组都会被忽略。
    """

    total = len(issues)
    if not isinstance(groups, list):
        return [dict(issue) for issue in issues], 0

    # 注意：assigned 里存的始终是**模型给的 1 起始编号**，
    # members 也是；换算成 0 起始只在最后重建时做一次。
    assigned: set[int] = set()
    keep_for: dict[int, int] = {}
    merged_count = 0

    for group in groups:
        if not isinstance(group, list):
            continue
        members: list[int] = []
        seen: set[int] = set()
        for raw in group:
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            if not (1 <= raw <= total) or raw in assigned or raw in seen:
                continue
            seen.add(raw)
            members.append(raw)
        if len(members) < 2:
            continue
        # 保留编号最小的那条：这样"保留者"永远排在前面，重建时可以一次扫完
        members.sort()
        assigned.update(members)
        keeper = members[0]
        for other in members[1:]:
            keep_for[other - 1] = keeper - 1
            merged_count += 1

    if not keep_for:
        return [dict(issue) for issue in issues], 0

    kept: list[dict[str, Any]] = []
    position_of: dict[int, int] = {}
    for position, issue in enumerate(issues):
        keeper = keep_for.get(position)
        if keeper is not None:
            target = kept[position_of[keeper]]
            if SEVERITY_ORDER.get(issue.get("severity"), 1) < SEVERITY_ORDER.get(
                target.get("severity"), 1
            ):
                target["severity"] = issue["severity"]
            for key in ("suggestion", "evidence", "location", "span_id", "span_label"):
                if not target.get(key) and issue.get(key):
                    target[key] = issue[key]
            continue
        position_of[position] = len(kept)
        kept.append(dict(issue))
    return kept, merged_count


def _cross_check_candidates(
    issues: list[dict[str, Any]],
    *,
    state: Any,
    guard: Any,
    emitter: Callable[[str], None],
    recorder: Any | None,
    run_id: str,
    iteration: int,
    document: str,
    spans: list[Span],
    settings: Settings,
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]], str]:
    """
    把所有分块的候选发现摆到一起做一次全局收敛。

    这一步补的正是"分块互相看不见"的缺口：并行跑的任何一个分块都不知道别人
    报了什么，所以同一个毛病可能被报好几遍，甚至两条发现互相打架。
    全局视角下每个候选都要和其它所有候选对一遍，比"后面的批次看到前面的发现"更强。

    失败一律降级：返回原候选、不合并、不补充，绝不让整个 Audit 挂掉。
    """

    try:
        check(state, agent="audit", stage="cross_check")
    except BudgetExceeded as exc:
        emitter(f"[Audit] 预算不足，跳过跨批对齐：{exc.reason}")
        return issues, 0, [], f"budget: {exc.reason}"

    fields = build_context(
        state,
        "audit",
        extra={
            "candidate_issues": issues,
            # 这一步只要候选清单，不要把整篇正文也塞进来
            "chunk_text": "",
            "instructions": "全局对齐：只合并重复、补充互相矛盾的问题，不要重新审查段落。",
        },
        budget_tokens=settings.context.audit_cross_check_tokens,
    )
    prompt = render_cross_check_prompt(format_context(fields))

    try:
        response = guard.run(
            partial(
                call_llm,
                prompt,
                "audit",
                system=CROSS_CHECK_SYSTEM,
                json_mode=True,
                run_id=run_id,
                action="cross_check",
            ),
            action="cross_check",
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            extra={"candidates": len(issues)},
        )
        data = parse_json_object(response.text, context="AuditCrossCheck")
    except Exception as exc:  # noqa: BLE001 - 对齐失败不影响已有结论
        emitter(f"[Audit] 跨批对齐失败，保留原候选：{type(exc).__name__}: {exc}")
        return issues, 0, [], str(exc)

    deduped, merged_count = _apply_duplicate_groups(issues, data.get("duplicates"))

    extra_issues: list[dict[str, Any]] = []
    for item in data.get("issues") or []:
        normalized = _normalize_issue(item, chunk=spans, document=document)
        if normalized is not None:
            normalized["source"] = "cross_check"
            extra_issues.append(normalized)

    return deduped, merged_count, extra_issues, ""


# ---------------------------------------------------------------- 定向对照

# "结论 / 总结"这类收口章节的标题关键词
_CONCLUSION_KEYS = ("结论", "总结", "结语", "conclusion", "conclusions", "summary")
_NUMBER_RE = re.compile(r"\d")
# 对照材料的大小上限，保证"小对照"真的小
MAX_COMPARISON_SENTENCES = 40
MAX_SECTION_TAIL_CHARS = 120
# 只取到二级标题（"## 3 实验"）：三级小节太碎，每个都取末段会把材料撑大
MAX_CHAPTER_LEVEL = 2
# 「强指标词」：出现就几乎可以断定这句在讲结果数值 —— 优先收
_METRIC_STRONG = (
    "%",
    "％",
    "准确率",
    "精确率",
    "精度",
    "召回",
    "F1",
    "ACC",
    "AUC",
    "误报",
    "漏报",
    "识别率",
)
# 「弱指标词」：只是"可能和数值有关" —— 配额不够时先丢这些
_METRIC_WEAK = (
    "提升",
    "下降",
    "提高",
    "降低",
    "达到",
    "误差",
    "均值",
    "平均",
    "相比",
    "优于",
)
_METRIC_HINTS = _METRIC_STRONG + _METRIC_WEAK


def _is_conclusion_span(span: Span) -> bool:
    section = re.sub(r"[\s#*·]+", "", str(getattr(span, "section", "") or ""))
    return any(key in section.lower() for key in _CONCLUSION_KEYS)


def _render_section_tails(spans: list[Span], *, skip_zones: set[str] | None = None) -> str:
    """
    各章的"标题 + 结尾一段"（截断）。

    用于对照"摘要是怎么承诺的、正文到底有没有产出"：
    章节标题能看出有没有对应的章节，结尾一段能看出这一章到底做了什么。

    两条过滤（都是踩过的坑）：

    - **代码注释不是章节标题**：论文里 `# 将序列切分为两半` 这种 Python 注释
      会被 spans 当成 Markdown 一级标题，于是每一条注释都变成一个"章"、
      还各自配一个"末段"。这里只认"以编号或白名单词开头"的真标题。
    - **表格不当末段**：章节末尾若是表格，整张 Markdown 表会被压成一行塞进材料。
    """

    skip = skip_zones or set()
    lines: list[str] = []
    tail: Span | None = None

    def flush() -> None:
        nonlocal tail
        if tail is None:
            return
        body = normalize_whitespace(tail.text)
        if len(body) > MAX_SECTION_TAIL_CHARS:
            body = body[:MAX_SECTION_TAIL_CHARS] + "…"
        lines.append(f"[{tail.label}] {body}")
        tail = None

    def heading_level(text: str) -> int:
        match = re.match(r"^(#{1,6})\s", str(text or ""))
        return len(match.group(1)) if match else 99

    for span in spans:
        if span.zone in skip:
            continue
        if span.kind == "heading":
            if not _is_chapter_heading(span.text):
                continue  # 代码注释型"标题"：跳过，也不打断当前章
            flush()
            lines.append(f"[{span.label}] {span.text}")
            continue
        if span.kind == "table":
            continue  # 表格不当"末段"
        section = getattr(span, "section", "") or ""
        if _is_chapter_heading(section) and heading_level(section) <= MAX_CHAPTER_LEVEL:
            tail = span
    flush()
    return "\n".join(lines)


# 真章节标题：`# 4 实验设计与实现`、`## 2.2 MoCo对比学习框架`、`# 摘 要`…
# 而 `# 将序列切分为两半`（Python 注释）不匹配 —— 它没有编号也不是白名单词。
_CHAPTER_HEAD_RE = re.compile(
    r"^#{0,6}\s*(?:"
    r"\d+(?:\.\d+)*"          # 4 / 2.2 / 2.2.1
    r"|第[一二三四五六七八九十]"
    r"|摘\s*要|中\s*文\s*摘\s*要|英\s*文\s*摘\s*要|ABSTRACT|Keywords?"
    r"|目\s*录|Contents"
    r"|致\s*谢|Acknowledg"
    r"|参考文献|References?"
    r"|附\s*录|Appendix"
    r"|结\s*论|总\s*结|结\s*语|Conclusion"
    r"|诚\s*信"
    r")",
    re.IGNORECASE,
)


def _is_chapter_heading(text: str) -> bool:
    """这一行（或这一段所属的 section）是不是真的章节标题。"""

    return bool(_CHAPTER_HEAD_RE.match(str(text or "").strip()))


def _render_numbered_sentences(
    spans: list[Span], *, limit: int = MAX_COMPARISON_SENTENCES
) -> tuple[str, int]:
    """
    正文里"含数字"的句子（不含表格）：专门用来和表格里的数值对照。

    返回 ``(文本, 因上限被丢弃的句数)``。

    两档优先级：先收**强指标词**（准确率/召回/F1/误报…）的句子，再收弱指标词
    （提升/相比/优于…）的。以前是一锅端按出现顺序取前 N 句，结果是
    "说了一句'优于'的废话"把"准确率 96.67%"这种关键句挤出配额。
    """

    strong: list[str] = []
    weak: list[str] = []
    for span in spans:
        if span.kind == "table":
            continue
        for sentence in span.sentences():
            if not _NUMBER_RE.search(sentence):
                continue
            line = f"[{span.label}] {sentence}"
            if any(hint in sentence for hint in _METRIC_STRONG):
                strong.append(line)
            elif any(hint in sentence for hint in _METRIC_WEAK):
                weak.append(line)

    ordered = strong + weak
    # 0 = 不限。注意不能写成 ordered[:0]（那会一句都不取，
    # 整组「正文数字 × 表格」对照会被静默跳过）。
    if limit <= 0:
        return "\n".join(ordered), 0
    kept = ordered[:limit]
    dropped = len(ordered) - len(kept)
    return "\n".join(kept), dropped


def _build_comparisons(auditable: list[Span]) -> list[dict[str, Any]]:
    """
    要做的定向对照：每一组都是"两份材料 + 只看什么"。

    关键点：材料都很短，而且**把要对照的两份摆在一起**。
    跨章节矛盾本质是对照任务——两句话隔着上万 token 时，模型两边都记不住。
    """

    comparisons: list[dict[str, Any]] = []
    abstract = [span for span in auditable if span.zone == "abstract"]
    conclusion = [span for span in auditable if _is_conclusion_span(span)]

    if abstract and conclusion:
        comparisons.append(
            {
                "key": "abstract_conclusion",
                "title": "摘要 × 结论",
                "focus": "数值、结论强度、方法与数据集数量是否一致",
                "left": render_annotated(abstract),
                "right": render_annotated(conclusion),
            }
        )

    if abstract:
        tails = _render_section_tails(auditable, skip_zones={"abstract", "references"})
        if tails.strip():
            comparisons.append(
                {
                    "key": "promise_outcome",
                    "title": "摘要的承诺 × 各章产出",
                    "focus": "摘要里说做了/提出了的东西，各章有没有对应内容",
                    "left": render_annotated(abstract),
                    "right": tails,
                }
            )

    numbers, dropped_numbers = _render_numbered_sentences(
        auditable, limit=get_settings().context.audit_compare_sentences
    )
    tables = render_annotated([span for span in auditable if span.kind == "table"])
    if numbers.strip() and tables.strip():
        comparisons.append(
            {
                "key": "numbers_tables",
                "title": "正文数字 × 表格",
                "focus": "同一指标、同一组实验的数值是否一致",
                "left": numbers,
                "right": tables,
                # 因 40 句上限被丢掉的句数 —— 必须报出来，不能装作查了全篇
                "dropped": dropped_numbers,
            }
        )
    return comparisons


def _run_targeted_checks(
    comparisons: list[dict[str, Any]],
    *,
    state: Any,
    auditable: list[Span],
    guard: Any,
    emitter: Callable[[str], None],
    recorder: Any | None,
    run_id: str,
    iteration: int,
    document: str,
    naming_hint: list[dict[str, Any]],
    section_gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    跑完所有定向对照。

    返回 (问题列表, 每组对照的执行情况)。对照彼此独立，可以并发；
    结果同样**按组序号**收集，保证问题顺序不随网络抖动变化。
    """

    meta = [
        {
            "key": item["key"],
            "title": item["title"],
            "found": 0,
            "error": "",
            "dropped": int(item.get("dropped") or 0),
        }
        for item in comparisons
    ]
    if not comparisons:
        return [], meta

    try:
        check(state, agent="audit", stage="targeted")
    except BudgetExceeded as exc:
        emitter(f"[Audit] 预算不足，跳过定向对照：{exc.reason}")
        for item in meta:
            item["error"] = f"budget: {exc.reason}"
        return [], meta

    hint = _consistency_hint(
        {span.label for span in auditable},
        naming=naming_hint,
        gaps=section_gaps,
        near_misses=near_misses,
    )

    def run_one(index: int, comparison: dict[str, Any]) -> tuple[int, list[dict[str, Any]], str]:
        material = (
            f"对照关系：{comparison['title']}\n"
            f"只看：{comparison['focus']}\n\n"
            f"=== 材料 A ===\n{comparison['left']}\n\n"
            f"=== 材料 B ===\n{comparison['right']}"
        )
        fields = build_context(
            state,
            "audit",
            extra={
                "chunk_text": material,
                "consistency_check": hint,
                "instructions": (
                    f"这是定向对照「{comparison['title']}」："
                    f"只找两份材料之间的矛盾（{comparison['focus']}），"
                    "不要评价文笔、不要提无关建议。"
                ),
            },
            budget_tokens=settings.context.audit_targeted_tokens,
        )
        prompt = render_targeted_check_prompt(format_context(fields))
        try:
            response = guard.run(
                partial(
                    call_llm,
                    prompt,
                    "audit",
                    system=TARGETED_CHECK_SYSTEM,
                    json_mode=True,
                    run_id=run_id,
                    action=f"targeted_{comparison['key']}",
                ),
                action=f"targeted_{comparison['key']}",
                recorder=recorder,
                run_id=run_id,
                iteration=iteration,
                extra={"comparison": comparison["key"]},
            )
            data = parse_json_object(response.text, context="AuditTargeted")
        except Exception as exc:  # noqa: BLE001 - 单组对照失败不影响其它
            emitter(
                f"[Audit] 定向对照「{comparison['title']}」失败："
                f"{type(exc).__name__}: {exc}"
            )
            return index, [], str(exc)

        issues: list[dict[str, Any]] = []
        for item in data.get("issues") or []:
            normalized = _normalize_issue(item, chunk=auditable, document=document)
            if normalized is not None:
                normalized["source"] = f"targeted:{comparison['key']}"
                issues.append(normalized)
        return index, issues, ""

    results: dict[int, tuple[list[dict[str, Any]], str]] = {}
    concurrency = max(1, min(int(settings.context.audit_concurrency or 1), len(comparisons)))
    if concurrency == 1:
        for index, comparison in enumerate(comparisons):
            _, issues, error = run_one(index, comparison)
            results[index] = (issues, error)
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="audit-cmp") as pool:
            futures = {
                pool.submit(run_one, index, comparison): index
                for index, comparison in enumerate(comparisons)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    _, issues, error = future.result()
                except Exception as exc:  # noqa: BLE001 - 兜底：工作线程自身异常
                    issues, error = [], str(exc)
                results[index] = (issues, error)

    collected: list[dict[str, Any]] = []
    for index in sorted(results):
        issues, error = results[index]
        if error:
            meta[index]["error"] = error
        meta[index]["found"] = len(issues)
        collected.extend(issues)
    return collected, meta


def _whole_paper_check(
    *,
    state: Any,
    guard: Any,
    emitter: Callable[[str], None],
    recorder: Any | None,
    run_id: str,
    iteration: int,
    document: str,
    auditable: list[Span],
    naming_hint: list[dict[str, Any]],
    section_gaps: list[dict[str, Any]],
    near_misses: list[dict[str, Any]],
    settings: Settings,
    known_issues: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """
    整篇送审，专查跨章节矛盾。

    逐段审查哪怕切得再细，也看不到"摘要说 95%、结论说 89%"这种跨越全文的矛盾——
    它天然需要同时看到首尾。所以这里不分块，把整篇放进一次调用。

    超长时按"头尾保留"截断：摘要与结论正好在首尾，也是矛盾最常出现的位置。

    ``known_issues`` 是**分块审查等环节已经发现的问题**。把它们一起交给模型、
    要求不要重复报——否则它那几条输出配额会被"错别字"这种局部问题占满，
    一条跨段矛盾都报不出来。

    返回 (问题列表, 是否被截断, 错误信息)。
    """

    try:
        check(state, agent="audit", stage="whole_paper")
    except BudgetExceeded as exc:
        emitter(f"[Audit] 预算不足，跳过全文矛盾核查：{exc.reason}")
        return [], False, f"budget: {exc.reason}"

    consistency_hint = _consistency_hint(
        {span.label for span in auditable},
        naming=naming_hint,
        gaps=section_gaps,
        near_misses=near_misses,
    )
    already: list[str] = []
    for issue in (known_issues or [])[:40]:
        label = issue.get("span_label") or issue.get("location") or "?"
        problem = str(issue.get("problem") or "").strip()[:60]
        if problem:
            already.append(f"- [{label}] {problem}")
    already_text = "\n".join(already)

    instructions = (
        "全文核查：只找**跨越两处、合起来看才发现**的矛盾（前后数值不一致、"
        "摘要与结论冲突、承诺与产出不符）。不要重复逐段审查已经报过的局部问题，"
        "不要评价文笔。"
    )
    if already_text:
        instructions += (
            "\n\n以下是其它环节**已经报过**的问题。请做两件事：\n"
            "① 把它们之外的跨段矛盾找出来（不要重复报这些本身）；\n"
            "② **补全它们的范围**——逐条对照全文，如果某条问题在别的位置也成立，\n"
            "   就输出一条对应条目，把**所有相关位置**（含原清单里那处）写进 span_labels，\n"
            "   并在 problem 里注明「这是对『<原问题摘要>』的范围补充」。只补位置，不重复描述问题。\n"
            + already_text
        )
    fields = build_context(
        state,
        "audit",
        extra={
            "chunk_text": render_annotated(auditable),
            "consistency_check": consistency_hint,
            "instructions": instructions,
        },
        budget_tokens=settings.context.audit_whole_paper_tokens,
    )
    prompt = render_whole_paper_prompt(format_context(fields))
    truncated = "（此处省略约" in fields.get("chunk_text", "")

    try:
        response = guard.run(
            partial(
                call_llm,
                prompt,
                "audit",
                system=WHOLE_PAPER_SYSTEM,
                json_mode=True,
                run_id=run_id,
                action="whole_paper",
            ),
            action="whole_paper",
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            extra={"blocks": len(auditable), "truncated": truncated},
        )
        data = parse_json_object(response.text, context="AuditWholePaper")
    except Exception as exc:  # noqa: BLE001 - 核查失败不影响已有结论
        emitter(f"[Audit] 全文矛盾核查失败：{type(exc).__name__}: {exc}")
        return [], truncated, str(exc)

    issues: list[dict[str, Any]] = []
    for item in data.get("issues") or []:
        normalized = _normalize_issue(item, chunk=auditable, document=document)
        if normalized is not None:
            normalized["source"] = "whole_paper"
            issues.append(normalized)
    return issues, truncated, ""


def _variation_issues(
    naming_hint: list[dict[str, Any]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """
    把"同一个名称有几种写法"变成**一条 decision 问题**。

    这条问题**由程序生成，不经过模型**，理由是两件事模型都做不好：

    - **位置要穷尽**（全文 40+ 处），模型在长文里列不全，输出也会被挤爆；
    - **选项要能执行**（"全文统一为 MOCO"），下游拿它做确定性替换，
      模型给的自由文本解析不出来。

    生成的问题带 `span_labels`（全部位置）和由变体直接生成的 `options`，
    用户在闸门上选一个，Revision 再据此全文替换。
    """

    issues: list[dict[str, Any]] = []
    for item in naming_hint[:limit]:
        variants = item.get("variants") or {}
        if len(variants) < 2:
            continue
        # 出现次数多的写法排前面（更可能是"正确写法"）
        tokens = sorted(variants, key=lambda token: (-len(variants[token]), token))

        labels: list[str] = []
        for token in tokens:
            labels.extend(variants[token])
        labels = list(dict.fromkeys(labels))
        if not labels:
            continue

        detail = "；".join(
            f"「{token}」出现在 {('、'.join(variants[token][:5]))}"
            + ("…" if len(variants[token]) > 5 else "")
            for token in tokens
        )
        issues.append(
            {
                "kind": "decision",
                "category": "专有名词",
                "severity": "medium",
                "span_label": labels[0],
                # 这条问题的**全部位置**：下游要按它把每一处都改掉
                "span_labels": labels,
                "location": "全文",
                "problem": (
                    f"同一个名称在全文有 {len(tokens)} 种写法，需要统一：{detail}。"
                    f"（全文共 {item.get('occurrences', len(labels))} 处）"
                ),
                "suggestion": "选定统一成哪一种，工具会把全文的其它写法一次替换掉",
                # 选项由**变体**直接生成，下游可以从选中的选项里可靠地取出目标写法
                "options": [f"全文统一为 {token}" for token in tokens],
                "source": "naming_variants",
            }
        )
    return issues


def run(
    state: Any,
    *,
    recorder: Any | None = None,
    logger: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """执行一次完整审查，返回写回 State 的字段。"""

    log = logger or print
    settings = get_settings()
    run_id = state.get("run_id", "adhoc")
    iteration = int(state.get("iteration", 0))
    document = state.get("current_document") or ""

    if not document.strip():
        log("[Audit] 论文内容为空，跳过审查。")
        return {
            "audit_findings": [],
            "audit_failed": False,
            "audit_meta": {"chunks": 0, "raw_issues": 0, "failed_chunks": 0},
            "spans": [],
        }

    version = int(state.get("document_version", 1))
    spans = build_spans(document, version=version)
    scope = state.get("audit_scope") or settings.workflow.audit_scope
    auditable = select_auditable(spans, scope) or spans
    # 先按段落切够，再按上限取：这样"因为太长而没审到"是可以被说出来的
    all_chunks = chunk_spans(
        auditable,
        max_tokens=settings.context.audit_chunk_tokens,
        max_chunks=None,
    )
    max_chunks = settings.context.audit_max_chunks
    chunks = all_chunks[:max_chunks] if max_chunks else all_chunks
    dropped_chunks = len(all_chunks) - len(chunks)

    # 确定性一致性检查（不吃"批次运气"）：写法不统一 / 只在总结里出现 / 疑似拼错
    # 术语一致性扫描：**排除代码段**。
    # 代码里的常量名/大小写（MOCO_QUEUE_SIZE、DATAset…）不归本文档的工具管——
    # 用户明确要求：代码一律不碰，连建议都不给。
    prose_spans = [span for span in auditable if not looks_like_code(span.text)]
    naming_hint = find_naming_variants(prose_spans)
    # 由扫描结果**直接生成**一条 decision 问题（带全部位置 + 可执行的选项），
    # 不指望模型把 40 多处列全。
    variation_issues = _variation_issues(naming_hint)
    # 补上 span_id（下游按 id 定位；缺了会在逐字核对那一步被丢掉）
    _by_label = {span.label: span for span in auditable}
    for _issue in variation_issues:
        _span = _by_label.get(str(_issue.get("span_label") or ""))
        if _span is not None:
            _issue["span_id"] = _span.id
    # 下面两类**刻意不启用**：
    #   find_near_miss_names —— 靠"和另一个写法只差一个字符 + 出现次数少"猜笔误。
    #     GNN/KNN、DIR/DIM、SVM/SIM 都是真实存在的独立术语，只差一个字母
    #     说明不了任何问题，这种"猜"每轮都会冒出来挡住复核，实测只会制造噪音。
    #   find_cross_section_gaps —— 靠"只在摘要出现、正文没出现"报问题。
    #     实测会误报（ADFA-LD 在正文有专门小节，只是专名被截成 ADFA），
    #     而且这属于"请作者核对"而不是缺陷，不该进问题清单。
    # 写法统一（MoCo/MOCO 这类同一个词的大小写差异）保留——那不是靠次数猜的。
    section_gaps: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []

    structure = structure_report(spans)
    log(
        f"[Audit] 文档共 {structure['blocks']} 块；按范围「{scope}」送审 "
        f"{len(auditable)} 块，分 {len(chunks)} 批"
        f"（跳过封面/目录/致谢等 {structure['blocks'] - len(auditable)} 块）。"
    )
    if dropped_chunks:
        log(
            f"[Audit] 注意：受上限 PRA_AUDIT_MAX_CHUNKS={max_chunks} 限制，"
            f"后面 {dropped_chunks} 批没有审到（整篇共 {len(all_chunks)} 批）。"
            "需要审全文请调大 PRA_AUDIT_MAX_CHUNKS 或 PRA_AUDIT_CHUNK_TOKENS。"
        )

    # 分块会并发执行：日志必须串行化输出（否则行与行会互相穿插），
    # Guard 也是按 Agent 共享的单例，通知统一走这个 emitter。
    emitter = _serialized_logger(log)
    guard = get_guard("audit", on_event=emitter)
    raw_issues: list[dict[str, Any]] = []
    failed_chunks = 0
    budget_stopped = False
    budget_snapshot: dict[str, Any] | None = None
    # 术语写法不统一这类**全文属性**的问题，由扫描直接给出，先入列
    raw_issues.extend(variation_issues)

    # 分块之间没有依赖（每块只读自己的文本），因此可以并行跑。
    # 注意：**按分块序号收集结果**，不是按完成顺序——否则 issue 的编号会随
    # 网络抖动变化，报告与用户看到的编号都不稳定。
    batch = _run_chunks(
        chunks,
        state=state,
        guard=guard,
        emitter=emitter,
        recorder=recorder,
        run_id=run_id,
        iteration=iteration,
        document=document,
        naming_hint=naming_hint,
        section_gaps=section_gaps,
        near_misses=near_misses,
        settings=settings,
    )
    for index in sorted(batch.outcomes):
        outcome = batch.outcomes[index]
        if outcome.failed:
            failed_chunks += 1
        raw_issues.extend(outcome.issues)
    if batch.budget_stopped:
        budget_stopped = True
        budget_snapshot = batch.budget_snapshot

    start_id = int(state.get("issue_counter", 0)) + 1
    # 专名核验：单独一次调用，判断"写法本身对不对"
    # （"阿里云 ESC"应为 ECS——正确写法根本不需要在文中出现过）
    if settings.context.audit_name_check:
        try:
            check(state, agent="audit", stage="name_check")
        except BudgetExceeded as exc:
            log(f"[Audit] 预算不足，跳过专名核验：{exc.reason}")
            budget_stopped = True
            budget_snapshot = budget_snapshot or exceeded_snapshot(state, exc)
        else:
            raw_issues.extend(
                _name_check_issues(
                    state,
                    spans=prose_spans,
                    log=emitter,
                    recorder=recorder,
                    iteration=iteration,
                    run_id=run_id,
                )
            )

    # 定向对照（主力）：把"要互相对照的两份材料"摆到一起查矛盾
    # （摘要 × 结论、摘要承诺 × 各章产出、正文数字 × 表格）。
    # 比整篇送审便宜一个数量级，而且对照任务本来就该把两份材料放在一起。
    targeted_meta: list[dict[str, Any]] = []
    if settings.context.audit_targeted:
        comparisons = _build_comparisons(auditable)
        targeted_issues, targeted_meta = _run_targeted_checks(
            comparisons,
            state=state,
            auditable=auditable,
            guard=guard,
            emitter=emitter,
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            document=document,
            naming_hint=naming_hint,
            section_gaps=section_gaps,
            near_misses=near_misses,
            settings=settings,
        )
        raw_issues.extend(targeted_issues)
        if targeted_meta:
            failed = [item["title"] for item in targeted_meta if item["error"]]
            dropped = sum(int(item.get("dropped") or 0) for item in targeted_meta)
            emitter(
                "[Audit] 定向对照 "
                + "、".join(item["title"] for item in targeted_meta)
                + f"：发现 {len(targeted_issues)} 处矛盾"
                + (f"；失败 {len(failed)} 组：{'、'.join(failed)}" if failed else "。")
            )
            if dropped:
                emitter(
                    f"[Audit] 注意：正文数字句有 {dropped} 句因上限"
                    f"（PRA_AUDIT_COMPARE_SENTENCES）未参与对照——"
                    "需要全量请调大该上限。"
                )

    # 整篇兜底：只在两种情况下跑 —— 文档足够短，或其它环节一条问题都没发现。
    # 默认不再无条件整篇送审：那是用最贵的输入去做对照任务。
    whole_meta = {
        "ran": False,
        "found": 0,
        "truncated": False,
        "error": "",
        "mode": "off" if not settings.context.audit_whole_paper else "skipped",
    }
    whole_mode = "off"
    if settings.context.audit_whole_paper:
        whole_tokens = estimate_tokens(render_annotated(auditable))
        small_doc = settings.context.audit_whole_paper_small_doc
        if small_doc and whole_tokens <= small_doc:
            whole_mode = "small_doc"
        elif not raw_issues:
            whole_mode = "fallback"
        else:
            whole_mode = "skipped"
            emitter(
                f"[Audit] 跳过整篇兜底（整篇约 {whole_tokens} tokens，"
                f"已有 {len(raw_issues)} 条发现，交给定向对照）。"
            )
        whole_meta["mode"] = whole_mode

    if settings.context.audit_whole_paper and whole_mode in {"small_doc", "fallback"}:
        whole_issues, truncated, error = _whole_paper_check(
            state=state,
            guard=guard,
            emitter=emitter,
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            document=document,
            auditable=auditable,
            naming_hint=naming_hint,
            section_gaps=section_gaps,
            near_misses=near_misses,
            settings=settings,
            # 把分块/专名/定向已经报过的问题交给整篇：
            # 否则它那 15 条输出配额会被"错别字"这类局部问题占满，
            # 一条跨段矛盾都报不出来。
            known_issues=raw_issues,
        )
        raw_issues.extend(whole_issues)
        whole_meta.update(
            ran=True,
            found=len(whole_issues),
            truncated=truncated,
            error=error,
            mode=whole_mode,
        )
        if not error:
            reason = (
                "文档较短，整篇送审"
                if whole_mode == "small_doc"
                else "其它环节没发现问题，兜底核查"
            )
            emitter(
                f"[Audit] 整篇兜底核查（{reason}）：发现 {len(whole_issues)} 处前后矛盾"
                + ("（文本过长，已按头尾保留截断）" if truncated else "。")
            )

    # 跨批对齐：把**全部**候选（分块 + 专名核验 + 全文核查）摆到一起做一次全局收敛，
    # 合并"说的是同一件事"的重复项、指出互相打架的条目。
    # 用户看到的仍是这一份合并后的清单——只有一道人工闸门。
    cross_meta = {"ran": False, "merged": 0, "added": 0, "error": ""}
    if settings.context.audit_cross_check and len(raw_issues) >= 2:
        raw_issues, merged_count, extra, error = _cross_check_candidates(
            raw_issues,
            state=state,
            guard=guard,
            emitter=emitter,
            recorder=recorder,
            run_id=run_id,
            iteration=iteration,
            document=document,
            spans=auditable,
            settings=settings,
        )
        raw_issues.extend(extra)
        cross_meta.update(
            ran=True, merged=merged_count, added=len(extra), error=error
        )
        if not error:
            emitter(
                f"[Audit] 跨批对齐完成：合并重复 {merged_count} 条，"
                f"补充跨批问题 {len(extra)} 条。"
            )

    merged = _merge_issues(
        raw_issues, limit=settings.context.max_issues, start_id=start_id
    )
    # 逐字核对证据 + 合并同类系统性问题 + 按严重程度取舍
    spans_by_id = {span.id: span for span in spans}
    merged, screen_meta = _screen(
        merged,
        spans_by_id=spans_by_id,
        min_severity=settings.context.audit_min_severity,
        max_issues=settings.context.audit_max_issues,
    )
    next_issue_counter = start_id + len(merged) - 1 if merged else start_id - 1

    # 用户明确说过“不用改”的问题：标出来，不再进入人工确认与补丁阶段
    rules = state.get("user_rules") or []
    ignored_count = 0
    for issue in merged:
        if rules and is_ignored(issue, rules):
            issue["ignored"] = True
            ignored_count += 1
    audit_failed = bool(chunks) and failed_chunks == len(chunks) and not merged

    meta = {
        "chunks": len(chunks),
        "failed_chunks": failed_chunks,
        "raw_issues": len(raw_issues),
        "merged_issues": len(merged),
        "blocks": len(spans),
        "scope": scope,
        "auditable_blocks": len(auditable),
        "chunks_total": len(all_chunks),
        "chunks_dropped": dropped_chunks,
        "naming_variants": len(naming_hint),
        "name_check": bool(settings.context.audit_name_check),
        "name_check_found": sum(
            1 for issue in merged if issue.get("source") == "name_check"
        ),
        "zones": structure["zones"],
        "skipped_noise": structure["skipped_noise"],
        "budget_stopped": budget_stopped,
        # 分块并发：实际生效的并发度（1 = 串行）
        "chunk_concurrency": batch.concurrency,
        # 跨批对齐（分块并行，谁都看不到别人，只能事后全局收敛）
        "cross_check": cross_meta["ran"],
        "cross_check_merged": cross_meta["merged"],
        "cross_check_added": cross_meta["added"],
        "cross_check_error": cross_meta["error"],
        # 定向对照（主力：把两份材料摆到一起查矛盾）
        "targeted": targeted_meta,
        "targeted_found": sum(int(item["found"]) for item in targeted_meta),
        # 整篇兜底核查（mode: small_doc / fallback / skipped / off）
        "whole_paper": whole_meta["ran"],
        "whole_paper_found": whole_meta["found"],
        "whole_paper_truncated": whole_meta["truncated"],
        "whole_paper_error": whole_meta["error"],
        "whole_paper_mode": whole_meta.get("mode", ""),
        "ignored_by_user": ignored_count,
        "dropped_unverifiable": screen_meta["unverifiable"],
        "dropped_code": screen_meta["code_skipped"],
        "dropped_low_severity": screen_meta["low_severity"],
        "merged_systematic": screen_meta["merged_systematic"],
        "high": sum(1 for issue in merged if issue["severity"] == "high"),
        "medium": sum(1 for issue in merged if issue["severity"] == "medium"),
        "low": sum(1 for issue in merged if issue["severity"] == "low"),
    }

    if audit_failed:
        log("[Audit] 所有批次的审查都失败了，标记为审查不可用。")
    else:
        located = sum(1 for issue in merged if issue.get("span_id"))
        note = f"，其中 {ignored_count} 个按用户要求自动忽略" if ignored_count else ""
        if screen_meta["unverifiable"] or screen_meta["low_severity"]:
            note += (
                f"（已丢弃无法逐字核对 {screen_meta['unverifiable']} 条、"
                f"低价值 {screen_meta['low_severity']} 条）"
            )
        log(
            f"[Audit] 审查完成，得到 {len(merged)} 个问题"
            f"（{located} 个已定位到段落）{note}。"
        )

    return {
        "audit_findings": merged,
        "audit_failed": audit_failed,
        "audit_meta": meta,
        "spans": index_summary(spans),
        "document_version": version,
        "issue_counter": max(int(state.get("issue_counter", 0)), next_issue_counter),
        "budget": budget_snapshot or usage(state),
    }


def _normalize_issue(
    item: Any,
    *,
    chunk: list[Span],
    document: str,
) -> dict[str, Any] | None:
    """规范化一条问题，并把段落标签解析成稳定 ID。"""

    if not isinstance(item, dict):
        return None

    problem = str(item.get("problem") or "").strip()
    if not problem:
        return None

    category = str(item.get("category") or "").strip()
    if category not in ISSUE_CATEGORIES:
        category = "语言表达"

    severity = str(item.get("severity") or "").strip().lower()
    if severity not in SEVERITIES:
        severity = "medium"

    # 三类：fix（确实写错，自动改）/ decision（前后冲突，需要作者拍板）/
    # advice（没写错但可以更好、以及所有代码问题，只写批注）。
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in ("fix", "decision", "advice"):
        kind = "fix"

    # decision 类给出可选项，让作者直接挑一个，不用自己组织语言
    raw_options = item.get("options") or []
    options: list[str] = []
    if isinstance(raw_options, list):
        for option in raw_options[:4]:
            text = str(option).strip()
            if text:
                options.append(text)

    # 矛盾的另一端：矛盾常常"报在 A、要改 B"（例如报在参数表 T10、要改正文 P183）。
    # 把另一处的段落 ID 一起带上，Revision 才能把那段正文放进上下文去改。
    raw_others = item.get("other_labels") or item.get("other") or []
    if isinstance(raw_others, str):
        raw_others = [piece.strip() for piece in raw_others.replace("，", ",").split(",")]
    other_labels: list[str] = []
    if isinstance(raw_others, list):
        for label in raw_others[:4]:
            text = str(label).strip()
            if text and text not in other_labels:
                other_labels.append(text)

    # 同一条问题涉及的**全部位置**（"不重复"指只写一条问题，不是只能有一个标号）
    raw_span_labels = item.get("span_labels") or []
    if isinstance(raw_span_labels, str):
        raw_span_labels = [
            piece.strip() for piece in raw_span_labels.replace("，", ",").split(",")
        ]
    span_labels: list[str] = []
    if isinstance(raw_span_labels, list):
        for label in raw_span_labels[:60]:
            text = str(label).strip()
            if text and text not in span_labels:
                span_labels.append(text)

    evidence = str(item.get("evidence") or "").strip()
    label = str(item.get("span_label") or item.get("span_id") or "").strip()
    span = resolve_span(chunk, span_id=label, label=label)

    # 标签没写对时，退一步：按 evidence 找到它所在的块
    if span is None and evidence:
        for candidate in chunk:
            if evidence and evidence in candidate.text:
                span = candidate
                break
    if span is None and evidence:
        best, score = None, 0.0
        for candidate in chunk:
            current = similarity(candidate.text, evidence)
            if current > score:
                best, score = candidate, current
        if best is not None and score >= 0.6:
            span = best

    location = str(item.get("location") or "").strip()
    if not location:
        location = span.section if span is not None else locate_section(document, evidence)

    issue: dict[str, Any] = {
        "kind": kind,
        "options": options if kind == "decision" else [],
        "other_labels": other_labels if kind == "decision" else [],
        "span_labels": span_labels,
        "category": category,
        "severity": severity,
        "location": location,
        "problem": problem,
        "evidence": evidence,
        "suggestion": str(item.get("suggestion") or "").strip(),
        "span_id": span.id if span is not None else "",
        "span_label": span.label if span is not None else label,
        "source": "audit",
    }
    if span is None:
        issue["warning"] = "未能定位到具体段落"
    return issue


def _merge_issues(
    issues: list[dict[str, Any]], *, limit: int = 30, start_id: int = 1
) -> list[dict[str, Any]]:
    """合并跨批次的重复问题，按严重程度排序，并统一编号。"""

    merged: list[dict[str, Any]] = []
    for issue in issues:
        duplicate = None
        for existing in merged:
            if similarity(existing["problem"], issue["problem"]) >= 0.82:
                duplicate = existing
                break
            if (
                issue.get("evidence")
                and existing.get("evidence")
                and similarity(existing["evidence"], issue["evidence"]) >= 0.9
            ):
                duplicate = existing
                break

        if duplicate is None:
            merged.append(dict(issue))
            continue

        if SEVERITY_ORDER[issue["severity"]] < SEVERITY_ORDER[duplicate["severity"]]:
            duplicate["severity"] = issue["severity"]
        for key in ("suggestion", "evidence", "location", "span_id", "span_label"):
            if not duplicate.get(key) and issue.get(key):
                duplicate[key] = issue[key]
        # 同一条问题在不同地方被报出来时，把**位置并集**：
        # 全篇审核补充的位置和分块报的位置合到一起，Revision 才不会只改一处。
        labels: list[str] = []
        for source in (duplicate, issue):
            for label in [source.get("span_label")] + list(
                source.get("span_labels") or []
            ):
                text = str(label or "").strip()
                if text and text not in labels:
                    labels.append(text)
        if labels:
            duplicate["span_labels"] = labels

    merged.sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], item.get("span_id", "")))
    for position, issue in enumerate(merged[:limit], start=start_id):
        issue["id"] = position
        issue["approved"] = False
    return merged[:limit]


def _screen(
    issues: list[dict[str, Any]],
    *,
    spans_by_id: dict[str, Span],
    min_severity: str = "medium",
    max_issues: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    审查后处理（这一步直接决定"会不会给一篇好论文报一堆问题"）：

    1. **逐字核对证据**：evidence 在对应段落里找不到 → 丢弃（防幻觉/防错位）；
    2. **同类系统性问题聚合**：同分类且问题描述高度相似的多条 → 合并成一条，
       并列出涉及的所有段落（"参考文献格式不统一（共 12 处：P581、P582…）"）；
    3. **严重程度门槛**：默认丢掉 low（可改可不改）；
    4. **总量上限**：超过上限时按严重程度截断。
    """

    meta = {
        "unverifiable": 0,
        "low_severity": 0,
        "merged_systematic": 0,
        "code_skipped": 0,
    }
    verified: list[dict[str, Any]] = []

    for issue in issues:
        span = spans_by_id.get(issue.get("span_id") or "")
        evidence = (issue.get("evidence") or "").strip()
        kind = issue.get("kind") or "fix"
        if span is None:
            meta["unverifiable"] += 1
            continue
        # 代码段不自动改：工具读到的是抽取文本，上下文不全，判断不了代码对错。
        # 但也不丢——降级成"建议"，只在 Word 里留一条批注给作者看。
        if looks_like_code(span.text):
            meta["code_skipped"] += 1
            issue["kind"] = "advice"
            kind = "advice"
        # 建议类不要求逐字引用原文（"建议补充 X 的来源"这种本来就没有可引的句子），
        # 但仍然要求定位到段落，批注才有地方挂。
        # 程序生成的条目（术语变体扫描）没有"逐字引用"这种东西——
        # 它的依据就是扫描结果本身，不该被逐字核对丢掉。
        if kind != "advice" and issue.get("source") not in (
            "naming_variants",
            "consistency",
        ):
            if len(evidence) < MIN_EVIDENCE_CHARS:
                meta["unverifiable"] += 1
                continue
            if normalize_whitespace(evidence) not in normalize_whitespace(span.text):
                meta["unverifiable"] += 1
                continue
        verified.append(issue)

    grouped = _merge_systematic(verified, meta)

    threshold = SEVERITY_RANK.get(min_severity, 1)
    kept = [
        issue
        for issue in grouped
        if SEVERITY_RANK.get(issue.get("severity", "medium"), 1) <= threshold
    ]
    meta["low_severity"] = len(grouped) - len(kept)

    kept.sort(key=lambda item: (SEVERITY_RANK.get(item["severity"], 2), item.get("span_label", "")))
    return kept[:max_issues], meta


def _merge_systematic(
    issues: list[dict[str, Any]], meta: dict[str, int]
) -> list[dict[str, Any]]:
    """把"同一类系统性毛病"合并成一条，避免同一个毛病被报 N 次。"""

    groups: list[dict[str, Any]] = []
    for issue in issues:
        matched = None
        for group in groups:
            if group["category"] != issue["category"]:
                continue
            if _same_kind(group["problem"], issue["problem"]):
                matched = group
                break
        if matched is None:
            item = dict(issue)
            item["affected_spans"] = [issue.get("span_label")] if issue.get("span_label") else []
            item["affected_ids"] = [issue.get("id")] if issue.get("id") is not None else []
            groups.append(item)
            continue

        if issue.get("span_label") and issue["span_label"] not in matched["affected_spans"]:
            matched["affected_spans"].append(issue["span_label"])
        if issue.get("id") is not None:
            matched["affected_ids"].append(issue["id"])
        # 保留更严重的等级
        if SEVERITY_RANK.get(issue["severity"], 2) < SEVERITY_RANK.get(matched["severity"], 2):
            matched["severity"] = issue["severity"]
        meta["merged_systematic"] += 1

    for group in groups:
        if len(group["affected_spans"]) > 1:
            others = "、".join(group["affected_spans"][1:6])
            suffix = f"（同类问题共 {len(group['affected_spans'])} 处，另见 {others}）"
            if suffix not in group["problem"]:
                group["problem"] = group["problem"].rstrip("。") + suffix
    return groups


def _same_kind(left: str, right: str) -> bool:
    """
    两条问题算不算"同一类系统性毛病"。

    只看整体相似度会漏：例如
      "参考文献格式不统一，缺少出版信息"
      "参考文献格式不统一，混用了两种著录方式"
    整体相似度不一定够，但开头完全一样。所以再加一条"共同前缀"判据。
    """

    left, right = left.strip(), right.strip()
    if not left or not right:
        return False
    if similarity(left, right) >= SYSTEMATIC_SIMILARITY:
        return True

    common = 0
    for first, second in zip(left, right):
        if first != second:
            break
        common += 1
    return common >= 6
