from typing import Annotated, Any
from urllib.parse import quote
import json
import logging
import os

from fastapi import FastAPI, Depends, Request, Response, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from sqlalchemy import text

from app.web import service
from app.web.settings import settings
from app.web.queue import login_rate_limit
from app.web.db import engine

app = FastAPI(title="论文修改工作台", version="0.1.0")
COOKIE = "paper_session"


@app.exception_handler(service.ApiError)
async def business_error(request, exc):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.middleware("http")
async def origin_check(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings().public_origin:
            return JSONResponse({"detail": "请求来源不受支持"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def current_user(request: Request):
    return await run_in_threadpool(service.identity, request.cookies.get(COOKIE, ""),
                                  request.headers.get("x-csrf-token"),
                                  write=request.method not in {"GET", "HEAD", "OPTIONS"})


async def admin(user=Depends(current_user)):
    if not user["is_admin"]:
        raise service.ApiError(403, "仅管理员可以执行此操作")
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


class LoginBody(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class DecisionBody(BaseModel):
    pending_id: str = Field(min_length=36, max_length=36)
    version: int = Field(ge=1)
    payload: dict[str, Any]


class NewUserBody(LoginBody):
    password: str = Field(min_length=12, max_length=128)
    daily_run_limit: int = Field(default=5, ge=1, le=1000)
    run_token_limit: int = Field(default=400000, ge=1000, le=5000000)


class UserUpdateBody(BaseModel):
    active: bool | None = None
    daily_run_limit: int | None = Field(default=None, ge=1, le=1000)
    run_token_limit: int | None = Field(default=None, ge=1000, le=5000000)


@app.get("/api/v1/health")
async def health():
    def check():
        with engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    try:
        await run_in_threadpool(check)
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.post("/api/v1/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    try:
        await run_in_threadpool(login_rate_limit, request.client.host, body.username)
    except service.ApiError:
        raise
    except Exception:
        raise service.ApiError(503, "登录服务暂时不可用") from None
    token, user = await run_in_threadpool(service.login, body.username, body.password)
    response.set_cookie(COOKIE, token, httponly=True, secure=settings().secure_cookie,
                        samesite="lax", max_age=settings().session_days * 86400, path="/")
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


@app.get("/api/v1/users/me")
async def me(user=Depends(current_user)):
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


@app.post("/api/v1/auth/logout")
async def logout(request: Request, response: Response, user=Depends(current_user)):
    await run_in_threadpool(service.logout, request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/v1/runs", status_code=202)
async def upload(file: Annotated[UploadFile, File()],
                 request: Annotated[str, Form()] = "全面检查并修改这篇论文",
                 rules: Annotated[str, Form()] = '["不要修改实验结果与数据"]',
                 user=Depends(current_user)):
    try:
        constraints = json.loads(rules)
        if not isinstance(constraints, list) or any(not isinstance(r, str) for r in constraints):
            raise ValueError()
    except (ValueError, TypeError):
        raise service.ApiError(400, "修改要求应为字符串列表") from None
    chunks, size = [], 0
    try:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings().upload_bytes:
                raise service.ApiError(413, "文件超过上传大小限制")
            chunks.append(chunk)
        try:
            return await run_in_threadpool(service.create_run, user["id"], b"".join(chunks), file.filename or "", request, constraints)
        except service.ApiError:
            raise
        except Exception:
            logging.exception("Upload storage/database operation failed")
            raise service.ApiError(503, "文件保存失败，请稍后再试") from None
    finally:
        await file.close()


@app.get("/api/v1/runs")
async def runs(user=Depends(current_user)):
    return await run_in_threadpool(service.list_runs, user["id"])


@app.get("/api/v1/runs/{run_id}")
async def run_detail(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.get_run, user["id"], run_id)


@app.get("/api/v1/runs/{run_id}/events")
async def events(run_id: str, cursor: int = 0, user=Depends(current_user)):
    return await run_in_threadpool(service.get_events, user["id"], run_id, max(0, cursor))


@app.post("/api/v1/runs/{run_id}/decisions", status_code=202)
async def decisions(run_id: str, body: DecisionBody, user=Depends(current_user)):
    return await run_in_threadpool(service.submit_decision, user["id"], run_id, body.pending_id, body.version, body.payload)


@app.post("/api/v1/runs/{run_id}/cancel")
async def cancel(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.cancel, user["id"], run_id)


@app.post("/api/v1/runs/{run_id}/retry", status_code=202)
async def retry(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.retry, user["id"], run_id)


@app.get("/api/v1/runs/{run_id}/download")
async def download(run_id: str, kind: str = "document", user=Depends(current_user)):
    if kind not in {"document", "report"}:
        raise service.ApiError(400, "未知的文件类型")
    data, name, mime = await run_in_threadpool(service.download, user["id"], run_id, kind)
    return Response(data, media_type=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/v1/admin/users")
async def users(user=Depends(admin)):
    return await run_in_threadpool(service.users)


@app.post("/api/v1/admin/users", status_code=201)
async def create_user(body: NewUserBody, user=Depends(admin)):
    return await run_in_threadpool(service.add_user, body.username, body.password,
                                  daily_runs=body.daily_run_limit, run_tokens=body.run_token_limit)


@app.patch("/api/v1/admin/users/{user_id}")
async def update_user(user_id: str, body: UserUpdateBody, user=Depends(admin)):
    return await run_in_threadpool(service.update_user, user_id, body.model_dump(exclude_none=True))
