import os

from fastapi import Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.web import service
from app.web.settings import settings

COOKIE = "paper_session"


async def current_user(request: Request):
    return await run_in_threadpool(
        service.identity,
        request.cookies.get(COOKIE, ""),
        request.headers.get("x-csrf-token"),
        write=request.method not in {"GET", "HEAD", "OPTIONS"},
    )


async def admin(user=Depends(current_user)):
    if not user["is_admin"]:
        raise service.ApiError(403, "仅管理员可以执行此操作")
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


def client_ip(request: Request):
    # Only enabled behind our private nginx, which overwrites this header.
    if os.getenv("PRA_TRUST_PROXY_IP", "0") == "1":
        return request.headers.get("x-real-ip") or request.client.host
    return request.client.host


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=settings().secure_cookie,
        samesite="lax",
        max_age=settings().session_days * 86400,
        path="/",
    )
