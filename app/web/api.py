"""FastAPI composition: middleware, error translation, health and routes."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.web import service
from app.web.db import engine
from app.web.routes import auth, runs, users
from app.web.settings import settings

app = FastAPI(title="论文修改工作台", version="0.1.0")


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


for router in (auth.router, runs.router, users.router):
    app.include_router(router, prefix="/api/v1")
