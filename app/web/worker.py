"""每个 Celery 执行片段运行到人工闸门或结束，随后释放 Worker。"""

import logging
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from botocore.exceptions import ClientError
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, text

from app.config import get_settings
from app.document.word_formats import CONVERSION_WARNING, WordFormatError, normalize_word
from app.state.schema import initial_state
from app.utils.budget import BudgetExceeded
from app.utils.call_policy import install_policy
from app.utils.loaders import load_bytes
from app.utils.rules import apply_payload, rule_texts
from app.utils.text import estimate_tokens
from app.utils.tracing import reset_recorders, utc_now
from app.web.db import engine, transaction
from app.web.models import Job, Run, new_id, now
from app.web.queue import acquire_llm_slot, celery_app, release_llm_slot
from app.web.service import event
from app.web.storage import DOCX_MIME, store
from app.workflow.harness import Harness, RunResult

_test_locks = {}
_test_locks_guard = threading.Lock()


@contextmanager
def exclusive_run(run_id):
    """会话级 advisory lock 保证同一论文不会被两个重复任务同时执行。"""
    if engine().dialect.name != "postgresql":
        with _test_locks_guard:
            lock = _test_locks.setdefault(run_id, threading.Lock())
        acquired = lock.acquire(False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return
    with engine().connect() as conn:
        acquired = conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:id, 0))"), {"id": run_id}
        )
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:id, 0))"), {"id": run_id}
                )
                conn.commit()


class WebCallPolicy:
    def __init__(self, run_id, previous_seconds=0, job_id=None, execution_token=None):
        self.run_id = run_id
        self.job_id, self.execution_token = job_id, execution_token
        self.started = time.monotonic()
        self.previous_seconds = previous_seconds

    def elapsed_seconds(self):
        return self.previous_seconds + time.monotonic() - self.started

    def reserve(self, agent, prompt, system, max_tokens):
        slot = acquire_llm_slot()
        amount = estimate_tokens(prompt + system) * 2 + max_tokens
        try:
            with transaction() as db:
                run = db.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
                if self.job_id and db.get(Job, self.job_id).execution_token != self.execution_token:
                    raise RuntimeError("该执行片段已由新的 Worker 接管")
                if run.status in {"cancel_requested", "cancelled"}:
                    raise RuntimeError("用户已取消任务")
                if run.used_tokens + run.reserved_tokens + amount > run.token_limit:
                    raise BudgetExceeded(
                        "本任务模型额度已用尽", {"exceeded": True, "reason": "本任务模型额度已用尽"}
                    )
                run.reserved_tokens += amount
                run.stage = agent
                messages = {
                    "audit": "正在检查论文",
                    "revision": "正在生成修改",
                    "review": "正在复核修改",
                    "orchestrator": "正在分析任务",
                }
                event(db, run, "progress", messages.get(agent, "正在处理"))
            return amount, slot
        except BaseException:
            release_llm_slot(slot)
            raise

    def settle(self, reservation, actual):
        amount, slot = reservation
        try:
            with transaction() as db:
                run = db.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
                if self.job_id and db.get(Job, self.job_id).execution_token != self.execution_token:
                    return
                run.reserved_tokens = max(0, run.reserved_tokens - amount)
                # 服务端未返回用量或异常时保守消耗预留额度，避免重试绕过额度。
                run.used_tokens += actual if actual else amount
                run.updated_at = now()
        finally:
            release_llm_slot(slot)


def heartbeat(job_id, policy, stop):
    while not stop.wait(10):
        try:
            with transaction() as db:
                job = db.get(Job, job_id)
                if (
                    job is None
                    or job.status != "running"
                    or job.execution_token != policy.execution_token
                ):
                    return
                job.lease_until = now() + timedelta(seconds=90)
                run = db.get(Run, job.run_id)
                run.processing_seconds = int(policy.elapsed_seconds())
                run.updated_at = now()
        except Exception:
            logging.exception("Job heartbeat failed")


def _initial_input(run, document, source):
    state = initial_state(
        run.request, document, run_id=run.id, max_iterations=get_settings().workflow.max_iterations
    )
    state["source_docx"] = str(source)
    state["visual_review"] = True
    state["turns"] = [
        {"turn": 1, "instruction": run.request, "created_at": utc_now(), "summary": ""}
    ]
    state["conversation"] = [{"turn": 1, "role": "user", "content": run.request}]
    rules, _ = apply_payload([], {"rules": run.rules}, turn=1, source="start")
    state["user_rules"], state["human_constraints"] = rules, rule_texts(rules)
    return state


def drive_workflow(run, job, harness):
    # Cache normalized input separately: retries and gate resumes must use the
    # exact same baseline even after a converter upgrade. Original stays intact.
    normalized_key = run.source_key + ".normalized.docx"
    try:
        source_data = store().get(normalized_key)
    except (FileNotFoundError, ClientError) as exc:
        if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") not in {
            "NoSuchKey",
            "404",
            "NotFound",
        }:
            raise
        source_data, _ = normalize_word(store().get(run.source_key), run.document_name)
        store().put(normalized_key, source_data, DOCX_MIME)
    run_dir = get_settings().paths.run_dir(run.id)
    run_dir.mkdir(parents=True, exist_ok=True)
    source = run_dir / "source.docx"
    source.write_bytes(source_data)
    # 已有 Checkpoint 时不重新 start，也不会触碰已应用的修改。
    snapshot = harness.graph.get_state(harness._config())
    state = dict(snapshot.values or {})
    if not state:
        loaded = load_bytes(source_data, filename="source.docx")
        if loaded.chars > 200000:
            raise ValueError("论文正文超过第一版处理长度限制")
        if not loaded.can_write_back:
            raise RuntimeError("文档无法生成修订版，请检查文档或安装完整 Word 依赖")
        initial = _initial_input(run, loaded.text, source)
        initial["input_warnings"] = (
            [] if run.document_name.lower().endswith(".docx") else [CONVERSION_WARNING]
        )
        return harness._invoke(initial)
    pending = harness.current_pending_request()
    if pending:
        if job.action == "resume" and pending == job.payload.get("request"):
            return harness.resume(job.payload["answer"])
        # 先前的片段已执行成功但业务回写前崩溃：发布真正的新闸门，禁止重复回答。
        return RunResult(
            run.id, harness.thread_id, "interrupted", state=state, interrupts=[pending]
        )
    if snapshot.next:
        return harness._invoke(None)
    return RunResult(run.id, harness.thread_id, "completed", state=state)


def execute_job(job_id, harness_factory=Harness):
    with transaction() as db:
        job = db.get(Job, job_id)
        if job is None or job.status in {"done", "failed"}:
            return
        run_id = job.run_id
    with exclusive_run(run_id) as acquired:
        if not acquired:
            return
        with transaction() as db:
            run = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            job = db.get(Job, job_id)
            if job.status in {"done", "failed"}:
                return
            if run.version != job.version or run.status in {
                "completed",
                "cancelled",
                "failed",
                "waiting_user",
            }:
                job.status, job.lease_until = "done", None
                return
            if run.status == "cancel_requested":
                run.status, job.status = "cancelled", "done"
                event(db, run, "cancelled", "任务已取消")
                return
            if job.attempts >= 3:
                run.status, job.status, run.error = (
                    "failed",
                    "failed",
                    "后台任务多次中断，请点击重试",
                )
                event(db, run, "failed", run.error)
                return
            # 前一个进程死亡后遗留的额度预留按保守用量结算。
            run.used_tokens += run.reserved_tokens
            run.reserved_tokens = 0
            job.attempts += 1
            job.status, job.lease_until = "running", now() + timedelta(seconds=90)
            job.execution_token = new_id()
            run.status, run.stage = "running", "planning"
            event(db, run, "running", "后台开始处理")
        policy = WebCallPolicy(run_id, run.processing_seconds, job_id, job.execution_token)
        stop = threading.Event()
        monitor = threading.Thread(target=heartbeat, args=(job_id, policy, stop), daemon=True)
        monitor.start()
        try:
            harness = harness_factory(thread_id=f"web-{run_id}", logger=lambda message: None)
            harness.run_id = run_id
            reset_recorders(run_id)
            with install_policy(run_id, policy):
                result = drive_workflow(run, job, harness)
            if result.status == "failed":
                raise RuntimeError(result.error or "Workflow 执行失败")
            preview = result.state.get("preview_manifest") or {}
            if preview.get("pages"):
                cache = get_settings().paths.run_dir(run.id) / "preview" / preview["fingerprint"]
                for page in preview["pages"]:
                    name = f"page-{page['number']}.png"
                    key = (
                        f"users/{run.user_id}/runs/{run.id}/preview/{preview['fingerprint']}/{name}"
                    )
                    if (cache / name).is_file():
                        store().put(key, (cache / name).read_bytes(), "image/png")
                    else:
                        # A resumed job may run on another worker. The frozen
                        # preview was uploaded before publishing its gate.
                        store().get(key)
            result_key = report_key = None
            if result.status == "completed":
                state = result.state
                if (state.get("redline_summary") or {}).get("error"):
                    raise RuntimeError("Word 修订文件生成失败，请重试或检查文档")
                target = Path(result.docx_path) if result.docx_path else None
                if target and target.exists():
                    document = target.read_bytes()
                elif not state.get("applied_patches") or not any(
                    p.get("patch_id") not in state.get("excluded_patch_ids", [])
                    for p in state.get("applied_patches", [])
                ):
                    document = store().get(run.source_key + ".normalized.docx")
                else:
                    raise RuntimeError("未生成有效的 Word 修订文件，请重试")
                from app.agents.report import build_report

                report = result.final_report or build_report(state)
                prefix = f"users/{run.user_id}/runs/{run_id}"
                result_key, report_key = f"{prefix}/revised.docx", f"{prefix}/report.md"
                store().put(result_key, document, DOCX_MIME)
                store().put(report_key, report.encode("utf-8"), "text/markdown; charset=utf-8")
            with transaction() as db:
                current = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                job_row = db.get(Job, job_id)
                if job_row.execution_token != policy.execution_token:
                    return
                current.processing_seconds = int(policy.elapsed_seconds())
                job_row.status, job_row.lease_until = "done", None
                if current.status in {"cancel_requested", "cancelled"}:
                    current.status, current.stage = "cancelled", "cancelled"
                    event(db, current, "cancelled", "任务已取消")
                    return
                if result.is_interrupted:
                    current.status, current.stage = "waiting_user", "waiting_user"
                    current.pending_id, current.pending_request = new_id(), result.pending_request
                    event(db, current, "waiting_user", "有问题需要你选择，请确认后继续")
                else:
                    current.status, current.stage = "completed", "done"
                    current.result_key, current.report_key = result_key, report_key
                    current.pending_id, current.pending_request = None, {}
                    event(db, current, "completed", "处理完成，可以下载修订版和报告")
                from app.document.review_items import location_map, readable

                locations = location_map(
                    result.state.get("current_document") or result.state.get("original_document")
                )
                current.state_summary = {
                    "review": result.review_status,
                    "changes": len(
                        (
                            result.state.get("final_patches")
                            if result.state.get("visual_review")
                            else result.state.get("applied_patches")
                        )
                        or []
                    ),
                    "excluded": len(result.state.get("excluded_patch_ids") or []),
                    "issues": len(result.state.get("audit_findings") or []),
                    "review_summary": readable(
                        (result.state.get("review_result") or {}).get("summary", ""), locations
                    ),
                    "warnings": list(result.state.get("input_warnings") or [])
                    + list(result.state.get("errors") or []),
                    "unfinished": result.state.get("unfinished_items") or [],
                    "reviewed_patch_ids": [
                        p["patch_id"] for p in result.state.get("final_patches") or []
                    ],
                    "reviewed_manual_ids": [
                        m["manual_id"] for m in result.state.get("final_manual_items") or []
                    ],
                    "preview": preview,
                    "written": (result.state.get("redline_summary") or {}).get("applied", 0),
                }
        except (Exception, SoftTimeLimitExceeded) as exc:
            logging.exception("Paper job %s failed", job_id)
            with transaction() as db:
                current = db.scalar(select(Run).where(Run.id == run_id).with_for_update())
                if db.get(Job, job_id).execution_token != policy.execution_token:
                    return
                current.status = (
                    "cancelled" if current.status in {"cancel_requested", "cancelled"} else "failed"
                )
                current.stage = current.status
                current.processing_seconds = int(policy.elapsed_seconds())
                current.error = (
                    (
                        str(exc)
                        if isinstance(exc, WordFormatError)
                        else "后台处理失败，可从保存的位置重试。"
                    )
                    if current.status == "failed"
                    else ""
                )
                db.get(Job, job_id).status = "failed"
                event(db, current, current.status, current.error or "任务已取消")
        finally:
            stop.set()
            monitor.join(timeout=5)


@celery_app.task(name="paper.execute")
def execute(job_id):
    import os

    if os.getenv("PRA_WEB_DEMO", "0") == "1":
        from app.web.demo import install_demo

        install_demo()
    execute_job(job_id)
