"""业务事务：登录、用户隔离、额度和人工决策。网络任务由 outbox 投递。"""
from datetime import timedelta, timezone
from hashlib import sha256
from pathlib import Path
import json
import secrets
import re

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select, func, delete

from app.web.db import transaction
from app.web.models import User, LoginSession, Run, Job, RunEvent, now, new_id
from app.web.settings import settings
from app.web.storage import store, validate_word_upload, DOCX_MIME
from app.workflow.interaction import prepare_response

hasher = PasswordHasher()
_dummy_hash = hasher.hash(secrets.token_urlsafe(24))
TERMINAL = {"completed", "failed", "cancelled"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def event(db, run: Run, kind: str, message: str):
    db.add(RunEvent(run_id=run.id, kind=kind, message=message))
    run.updated_at = now()


def user_json(user: User):
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin,
            "active": user.active, "daily_run_limit": user.daily_run_limit,
            "run_token_limit": user.run_token_limit}


def add_user(username: str, password: str, *, admin=False, daily_runs=None, run_tokens=None):
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@-]{2,79}", username):
        raise ApiError(400, "账号需为 3–80 位字母、数字或 _ . @ -")
    if not 12 <= len(password) <= 128:
        raise ApiError(400, "密码需为 12–128 个字符")
    with transaction() as db:
        if db.scalar(select(User).where(User.username == username)):
            raise ApiError(409, "账号已存在")
        user = User(username=username, password_hash=hasher.hash(password), is_admin=admin,
                    daily_run_limit=daily_runs or settings().daily_runs,
                    run_token_limit=run_tokens or settings().run_tokens)
        db.add(user)
        db.flush()
        return user_json(user)


def login(username: str, password: str):
    with transaction() as db:
        user = db.scalar(select(User).where(User.username == username.strip().lower()))
        try:
            valid = hasher.verify(user.password_hash if user else _dummy_hash, password)
        except VerificationError:
            valid = False
        if not valid or user is None or not user.active:
            raise ApiError(401, "账号或密码不正确")
        token, csrf = secrets.token_urlsafe(32), secrets.token_hex(32)
        db.add(LoginSession(token_hash=sha256(token.encode()).hexdigest(), user_id=user.id,
                            csrf_token=csrf, expires_at=now() + timedelta(days=settings().session_days)))
        return token, {**user_json(user), "csrf_token": csrf}


def identity(token: str, csrf: str | None = None, *, write=False):
    with transaction() as db:
        session = db.get(LoginSession, sha256(token.encode()).hexdigest())
        if session is None or aware(session.expires_at) <= now():
            raise ApiError(401, "请登录后继续")
        user = db.get(User, session.user_id)
        if user is None or not user.active:
            raise ApiError(401, "账号不可用")
        if write and not secrets.compare_digest(csrf or "", session.csrf_token):
            raise ApiError(403, "会话校验失败，请刷新页面")
        return {**user_json(user), "csrf_token": session.csrf_token}


def logout(token: str):
    with transaction() as db:
        row = db.get(LoginSession, sha256(token.encode()).hexdigest())
        if row:
            db.delete(row)


def reset_password(username: str, password: str):
    """仅供管理命令使用；改密码时撤销此账号的全部会话。"""
    if not 12 <= len(password) <= 128:
        raise ApiError(400, "密码需为 12–128 个字符")
    with transaction() as db:
        user = db.scalar(select(User).where(User.username == username.strip().lower()).with_for_update())
        if user is None:
            raise ApiError(404, "用户不存在")
        user.password_hash = hasher.hash(password)
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def owned(db, user_id, run_id, *, lock=False):
    query = select(Run).where(Run.id == run_id, Run.user_id == user_id)
    run = db.scalar(query.with_for_update() if lock else query)
    if run is None:
        raise ApiError(404, "任务不存在")
    return run


def run_json(run: Run):
    return {"id": run.id, "status": run.status, "stage": run.stage, "version": run.version,
            "document_name": run.document_name, "request": run.request,
            "used_tokens": run.used_tokens, "token_limit": run.token_limit,
            "pending": {"id": run.pending_id, "request": run.pending_request} if run.status == "waiting_user" else None,
            "summary": run.state_summary, "error": run.error,
            "has_result": bool(run.result_key), "has_report": bool(run.report_key),
            "created_at": aware(run.created_at).isoformat(), "updated_at": aware(run.updated_at).isoformat()}


def create_run(user_id: str, data: bytes, filename: str, request: str, rules: list[str]):
    try:
        name = validate_word_upload(data, filename)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if not request.strip() or len(request) > 4000 or len(rules) > 30 or any(len(r) > 1000 for r in rules):
        raise ApiError(400, "任务描述或修改要求过长")
    key = f"users/{user_id}/runs/{new_id()}/source{Path(name).suffix.lower()}"
    try:
        with transaction() as db:
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            day = now().astimezone(timezone(timedelta(hours=8))).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            used = db.scalar(select(func.count()).select_from(Run).where(Run.user_id == user_id, Run.created_at >= day))
            if used >= user.daily_run_limit:
                raise ApiError(429, f"今日任务额度已用完（{user.daily_run_limit} 次）")
            store().put(key, data, DOCX_MIME if name.lower().endswith(".docx") else "application/octet-stream")
            run = Run(user_id=user_id, document_name=name, source_key=key,
                      request=request.strip(), rules=rules, token_limit=user.run_token_limit)
            db.add(run)
            db.flush()
            db.add(Job(run_id=run.id, version=run.version, action="start"))
            event(db, run, "queued", "文件已上传，等待处理")
            return run_json(run)
    except Exception:
        # 只清理本次随机 key，数据库和对象存储之间不能使用同一个事务。
        try:
            store().delete(key)
        except Exception:
            pass
        raise


def list_runs(user_id: str):
    with transaction() as db:
        return [run_json(r) for r in db.scalars(select(Run).where(Run.user_id == user_id).order_by(Run.created_at.desc()).limit(100))]


def get_run(user_id: str, run_id: str):
    with transaction() as db:
        return run_json(owned(db, user_id, run_id))


def get_events(user_id: str, run_id: str, cursor=0):
    with transaction() as db:
        owned(db, user_id, run_id)
        rows = db.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.id > cursor).order_by(RunEvent.id).limit(100)).all()
        return {"cursor": rows[-1].id if rows else cursor, "events": [
            {"id": r.id, "kind": r.kind, "message": r.message, "created_at": aware(r.created_at).isoformat()} for r in rows]}


def submit_decision(user_id, run_id, pending_id, version, payload):
    if len(json.dumps(payload, ensure_ascii=False)) > 64000:
        raise ApiError(400, "回答内容过长")
    with transaction() as db:
        run = owned(db, user_id, run_id, lock=True)
        previous = db.scalar(select(Job).where(Job.run_id == run_id, Job.version == version + 1))
        if previous and previous.payload.get("pending_id") == pending_id:
            if previous.payload.get("original_answer") == payload:
                return run_json(run)
            raise ApiError(409, "此问题已经提交过不同答案")
        if run.status != "waiting_user" or run.pending_id != pending_id or run.version != version:
            raise ApiError(409, "问题状态已变化，请刷新后再提交")
        try:
            answer = prepare_response(run.pending_request, payload)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
        run.version += 1
        db.add(Job(run_id=run.id, version=run.version, action="resume", payload={
            "pending_id": pending_id, "request": run.pending_request, "answer": answer,
            "original_answer": payload}))
        run.status, run.stage = "queued", "queued"
        event(db, run, "queued", "选择已保存，等待继续处理")
        return run_json(run)


def cancel(user_id, run_id):
    with transaction() as db:
        run = owned(db, user_id, run_id, lock=True)
        if run.status in TERMINAL or run.status == "cancel_requested":
            return run_json(run)
        run.status = "cancel_requested" if run.status == "running" else "cancelled"
        run.stage = run.status
        event(db, run, run.status, "已请求停止；正在执行的模型调用结束后会停止" if run.status == "cancel_requested" else "任务已取消")
        return run_json(run)


def retry(user_id, run_id):
    with transaction() as db:
        run = owned(db, user_id, run_id, lock=True)
        if run.status != "failed":
            raise ApiError(409, "只有失败任务可以重试")
        previous = db.scalar(select(Job).where(Job.run_id == run_id).order_by(Job.version.desc()))
        run.version += 1
        run.status, run.stage, run.error = "queued", "queued", ""
        db.add(Job(run_id=run_id, version=run.version, action=previous.action, payload=previous.payload))
        event(db, run, "queued", "正在从保存的位置重试")
        return run_json(run)


def download(user_id, run_id, kind):
    with transaction() as db:
        run = owned(db, user_id, run_id)
        key = run.result_key if kind == "document" else run.report_key
        if run.status != "completed" or not key:
            raise ApiError(409, "文件尚未生成")
        name = f"{document_stem(run.document_name)}-修订版.docx" if kind == "document" else "修改报告.md"
    return store().get(key), name, DOCX_MIME if kind == "document" else "text/markdown; charset=utf-8"


def document_stem(name):
    return Path(name).stem


def users():
    with transaction() as db:
        return [user_json(u) for u in db.scalars(select(User).order_by(User.created_at.desc()))]


def update_user(user_id, values):
    with transaction() as db:
        user = db.get(User, user_id)
        if not user:
            raise ApiError(404, "用户不存在")
        for key in ("active", "daily_run_limit", "run_token_limit"):
            if key in values and values[key] is not None:
                setattr(user, key, values[key])
        return user_json(user)
