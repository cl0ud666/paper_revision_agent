"""
Audit Agent：系统性发现问题。

流程：

    论文 → 切成可寻址的块（span） → 按块分组
         → ① 并发逐块审查（ThreadPoolExecutor）
         → ② 跨批对齐（把全部分块的发现摆到一起，合并重复 / 指出互相矛盾）
         → ③ 全文矛盾核查（不分块，整篇送审，专查前后打架）
         → ④ 逐字核对证据 + 合并去重 + 按严重程度取舍

与上一版的区别：**分块按段落边界切**（不是按字符切），
因此模型看到的每个块都带段落ID（``[P03]``），
它给出的每条问题也都会带上这个ID——后续Revision才能精确落补丁。

为什么是"并发 + 事后全局对齐"，而不是"后面的批次看前面的发现"：
分块之间没有依赖（每块只读自己的文本），所以可以并行；但也正因为并行，
任何一块都看不到别的块报了什么。串行的"后者看前者"只有后面的能看到前面的，
全局对齐则让每个发现都要和其它所有发现对一遍，覆盖更全。

硬约束：evidence必须逐字来自该段落，这样补丁校验能一次通过。
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from app.agents.audit_context import (
    _build_comparisons as _build_comparisons,
    _consistency_hint as _consistency_hint,
    _is_chapter_heading as _is_chapter_heading,
    _is_conclusion_span as _is_conclusion_span,
    _name_candidates as _name_candidates,
    _name_context_snippet as _name_context_snippet,
    _render_name_list as _render_name_list,
    _render_numbered_sentences as _render_numbered_sentences,
    _render_section_tails as _render_section_tails,
)

# Compatibility exports keep existing integrations working during module migration.
from app.agents.audit_findings import (
    MIN_EVIDENCE_CHARS,
    _merge_issues as _merge_issues,
    _merge_systematic as _merge_systematic,
    _normalize_issue as _normalize_issue,
    _same_kind as _same_kind,
    _screen as _screen,
)
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
    select_auditable,
    structure_report,
)
from app.state.schema import SEVERITY_ORDER
from app.utils.budget import BudgetExceeded, check, exceeded_snapshot, usage
from app.utils.consistency import (
    find_naming_variants,
)
from app.utils.context import build_context, format_context
from app.utils.json_utils import parse_json_object
from app.utils.llm import call_llm
from app.utils.resilience import get_guard
from app.utils.rules import is_ignored
from app.utils.text import (
    estimate_tokens,
    looks_like_code,
)


def _name_check_issues(
    state: Any,
    *,
    spans: list[Span],
    log: Callable[[str], None],
    recorder: Any | None,
    iteration: int,
    run_id: str,
) -> list[dict[str, Any]]:
    """术语只在有段落语义依据时判错，不由频次或编辑距离直接生成问题。"""
    candidates = _name_candidates(spans)
    if not candidates:
        return []
    fields = build_context(state, "audit", extra={"name_list": _render_name_list(candidates)})
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
    except Exception as exc:  # 核验失败不能退回词频猜错
        log(f"[Audit] 专名核验失败（{type(exc).__name__}: {exc}），本次不生成术语错误结论。")
        return []

    known = {item["name"]: {c["span_label"] for c in item["contexts"]} for item in candidates}
    by_label = {span.label: span for span in spans}
    issues, seen = [], set()
    for item in data.get("suspected") or []:
        if not isinstance(item, dict):
            continue
        form = str(item.get("name") or "").strip()
        correct = str(item.get("correct") or "").strip()
        label = str(item.get("span_label") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        reason = str(item.get("reason") or "").strip()
        basis = str(item.get("context_basis") or "").strip()
        span = by_label.get(label)
        if (
            not form
            or not correct
            or form == correct
            or form not in known
            or label not in known[form]
            or span is None
            or str(item.get("confidence") or "").lower() != "high"
        ):
            continue
        # A token or invented quotation is not contextual evidence.
        if (
            len(evidence) < MIN_EVIDENCE_CHARS
            or evidence not in span.text
            or not re.search(r"(?<![A-Za-z0-9_])" + re.escape(form) + r"(?![A-Za-z0-9_])", evidence)
            or not reason
            or not basis
            or basis not in span.text
            or len(basis.replace(form, "").strip()) < 4
        ):
            continue
        # Even a "high confidence" response cannot use frequency/edit distance
        # as its justification. Both fields are checked, not just the prompt.
        if re.search(
            r"只出现|仅出现|出现.{0,8}次|高频|低频|多数|少数|罕见|只差.{0,4}(字符|字母)|编辑距离|疑似笔误|疑似拼写",
            reason,
        ):
            continue
        identity = (form, correct, label)
        if identity in seen:
            continue
        seen.add(identity)
        issues.append(
            {
                "kind": "fix",
                "category": "专有名词",
                "severity": "medium",
                "location": span.section or span.label,
                "problem": f"此处「{form}」与段落语义不符，建议改为「{correct}」：{reason}",
                "evidence": evidence,
                "context_basis": basis,
                "suggestion": f"仅在 [{label}] 引用的这处上下文中将「{form}」改为「{correct}」；其他位置需分别核对",
                "span_id": span.id,
                "span_label": span.label,
                "source": "name_check",
            }
        )
    log(
        f"[Audit] 专名核验：核了 {len(candidates)} 个专名，确认 {len(issues)} 处有上下文依据的问题。"
    )
    return issues


def _serialized_logger(log: Callable[[str], None]) -> Callable[[str], None]:
    """
    把logger包成线程安全的。

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
    # 实际生效的并发度（受PRA_AUDIT_CONCURRENCY与分块总数共同约束）
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
    settings: Settings,
) -> _ChunkBatch:
    """
    跑完所有分块的审查。

    - 并发度取``PRA_AUDIT_CONCURRENCY``，并受分块总数约束
      （不会为3个分块开6个线程）；
    - 预算检查放在**提交环节**（串行）：预算耗尽就不再提交剩余分块，
      保留"超预算降级而不是崩"的原有语义；
    - 单个分块失败只记一次失败，不影响其它分块；
    - 结果按分块序号收集，保证issue编号不随网络抖动变化。
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
            emitter(f"[Audit] 并发审查 {len(futures)}/{total} 个分块（并发度 {concurrency}）。")
        for future in as_completed(futures):
            index = futures[future]
            try:
                batch.outcomes[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - 兜底：工作线程自身的异常
                batch.outcomes[index] = _ChunkOutcome(index=index, failed=True, error=str(exc))
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

    # 注意：assigned里存的始终是**模型给的1起始编号**，
    # members也是；换算成0起始只在最后重建时做一次。
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

    失败一律降级：返回原候选、不合并、不补充，绝不让整个Audit挂掉。
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
    )

    def run_one(index: int, comparison: dict[str, Any]) -> tuple[int, list[dict[str, Any]], str]:
        material = (
            f"对照关系：{comparison['title']}\n"
            f"只看：{comparison['focus']}\n\n"
            f"=== 材料A ===\n{comparison['left']}\n\n"
            f"=== 材料B ===\n{comparison['right']}"
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
            emitter(f"[Audit] 定向对照「{comparison['title']}」失败：{type(exc).__name__}: {exc}")
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
    settings: Settings,
    known_issues: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """
    整篇送审，专查跨章节矛盾。

    逐段审查哪怕切得再细，也看不到"摘要说95%、结论说89%"这种跨越全文的矛盾——
    它天然需要同时看到首尾。所以这里不分块，把整篇放进一次调用。

    超长时按"头尾保留"截断：摘要与结论正好在首尾，也是矛盾最常出现的位置。

    ``known_issues``是**分块审查等环节已经发现的问题**。把它们一起交给模型、
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
            "   就输出一条对应条目，把**所有相关位置**（含原清单里那处）写进span_labels，\n"
            "   并在problem里注明「这是对『<原问题摘要>』的范围补充」。只补位置，不重复描述问题。\n"
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


def _variation_issues(naming_hint: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    """
    把"同一个名称有几种写法"变成**一条decision问题**。

    这条问题**由程序生成，不经过模型**，理由是两件事模型都做不好：

    - **位置要穷尽**（全文40+ 处），模型在长文里列不全，输出也会被挤爆；
    - **选项要能执行**（"全文统一为MOCO"），下游拿它做确定性替换，
      模型给的自由文本解析不出来。

    生成的问题带`span_labels`（全部位置）和由变体直接生成的`options`，
    用户在闸门上选一个，Revision再据此全文替换。
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
    """执行一次完整审查，返回写回State的字段。"""

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

    # 确定性术语一致性扫描（不吃"批次运气"），**排除代码段**。
    # 代码里的常量名/大小写（MOCO_QUEUE_SIZE、DATAset…）不归本文档的工具管——
    # 用户明确要求：代码一律不碰，连建议都不给。
    prose_spans = [span for span in auditable if not looks_like_code(span.text)]
    naming_hint = find_naming_variants(prose_spans)
    # 由扫描结果**直接生成**一条decision问题（带全部位置 + 可执行的选项），
    # 不指望模型把40多处列全。
    variation_issues = _variation_issues(naming_hint)
    # 补上span_id（下游按id定位；缺了会在逐字核对那一步被丢掉）
    _by_label = {span.label: span for span in auditable}
    for _issue in variation_issues:
        _span = _by_label.get(str(_issue.get("span_label") or ""))
        if _span is not None:
            _issue["span_id"] = _span.id
    structure = structure_report(spans)
    log(
        f"[Audit] 文档共 {structure['blocks']} 块；按范围「{scope}」送审 "
        f"{len(auditable)} 块，分 {len(chunks)} 批"
        f"（跳过封面/目录/致谢等 {structure['blocks'] - len(auditable)} 块）。"
    )
    if dropped_chunks:
        log(
            f"[Audit] 注意：受上限PRA_AUDIT_MAX_CHUNKS={max_chunks} 限制，"
            f"后面 {dropped_chunks} 批没有审到（整篇共 {len(all_chunks)} 批）。"
            "需要审全文请调大PRA_AUDIT_MAX_CHUNKS或PRA_AUDIT_CHUNK_TOKENS。"
        )

    # 分块会并发执行：日志必须串行化输出（否则行与行会互相穿插），
    # Guard也是按Agent共享的单例，通知统一走这个emitter。
    emitter = _serialized_logger(log)
    guard = get_guard("audit", on_event=emitter)
    raw_issues: list[dict[str, Any]] = []
    failed_chunks = 0
    budget_stopped = False
    budget_snapshot: dict[str, Any] | None = None
    # 术语写法不统一这类**全文属性**的问题，由扫描直接给出，先入列
    raw_issues.extend(variation_issues)

    # 分块之间没有依赖（每块只读自己的文本），因此可以并行跑。
    # 注意：**按分块序号收集结果**，不是按完成顺序——否则issue的编号会随
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
    # （"阿里云ESC"应为ECS——正确写法根本不需要在文中出现过）
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
            settings=settings,
            # 把分块/专名/定向已经报过的问题交给整篇：
            # 否则它那15条输出配额会被"错别字"这类局部问题占满，
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
        cross_meta.update(ran=True, merged=merged_count, added=len(extra), error=error)
        if not error:
            emitter(
                f"[Audit] 跨批对齐完成：合并重复 {merged_count} 条，补充跨批问题 {len(extra)} 条。"
            )

    from app.document.citations import citation_consistency_audit

    raw_issues.extend(citation_consistency_audit(document))
    merged = _merge_issues(raw_issues, limit=settings.context.max_issues, start_id=start_id)
    # 逐字核对证据 + 合并同类系统性问题 + 按严重程度取舍
    spans_by_id = {span.id: span for span in spans}
    merged, screen_meta = _screen(
        merged,
        spans_by_id=spans_by_id,
        min_severity=settings.context.audit_min_severity,
        max_issues=settings.context.audit_max_issues,
    )
    # Group by meaning, not the wording of two separate paragraph complaints.
    # This runs before the persisted choice gate, never while replaying an
    # already-presented pending request (option indexes must remain stable).
    from app.document.terminology import normalize_terminology_decisions

    merged = normalize_terminology_decisions(
        merged, document, state.get("user_rules") or [], version=version
    )
    next_issue_counter = max((int(i["id"]) for i in merged), default=start_id - 1)

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
        "name_check_found": sum(1 for issue in merged if issue.get("source") == "name_check"),
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
        log(f"[Audit] 审查完成，得到 {len(merged)} 个问题（{located} 个已定位到段落）{note}。")

    return {
        "audit_findings": merged,
        "audit_failed": audit_failed,
        "audit_meta": meta,
        "spans": index_summary(spans),
        "document_version": version,
        "issue_counter": max(int(state.get("issue_counter", 0)), next_issue_counter),
        "budget": budget_snapshot or usage(state),
    }
