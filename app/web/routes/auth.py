import os

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from app.web import service
from app.web.dependencies import COOKIE, client_ip, current_user, set_session_cookie
from app.web.queue import login_rate_limit, registration_rate_limit
from app.web.schemas import LoginBody, RegisterBody
from app.web.settings import settings

router = APIRouter()


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    try:
        await run_in_threadpool(login_rate_limit, client_ip(request), body.username)
    except service.ApiError:
        raise
    except Exception:
        raise service.ApiError(503, "登录服务暂时不可用") from None
    token, user = await run_in_threadpool(service.login, body.username, body.password)
    set_session_cookie(response, token)
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


@router.get("/auth/options")
async def auth_options():
    return {"registration_open": settings().registration_open, "free_runs": settings().free_runs}


@router.post("/auth/register", status_code=201)
async def register(body: RegisterBody, request: Request, response: Response):
    if not settings().registration_open:
        raise service.ApiError(403, "注册暂未开放，请联系管理员")
    try:
        await run_in_threadpool(registration_rate_limit, client_ip(request))
    except service.ApiError:
        raise
    except Exception:
        raise service.ApiError(503, "注册服务暂时不可用") from None
    token, user = await run_in_threadpool(service.register, body.username, body.password)
    set_session_cookie(response, token)
    return user


@router.get("/users/me")
async def me(user=Depends(current_user)):
    return {**user, "demo": os.getenv("PRA_WEB_DEMO", "0") == "1"}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, user=Depends(current_user)):
    await run_in_threadpool(service.logout, request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
