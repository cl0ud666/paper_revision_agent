import json
import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.web import service
from app.web.dependencies import current_user
from app.web.schemas import DecisionBody
from app.web.settings import settings

router = APIRouter()


@router.post("/runs", status_code=202)
async def upload(
    file: Annotated[UploadFile, File()],
    request: Annotated[str, Form()] = "全面检查并修改这篇论文",
    rules: Annotated[str, Form()] = '["不要修改实验结果与数据"]',
    user=Depends(current_user),
):
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
            return await run_in_threadpool(
                service.create_run,
                user["id"],
                b"".join(chunks),
                file.filename or "",
                request,
                constraints,
            )
        except service.ApiError:
            raise
        except Exception:
            logging.exception("Upload storage/database operation failed")
            raise service.ApiError(503, "文件保存失败，请稍后再试") from None
    finally:
        await file.close()


@router.get("/runs")
async def runs(user=Depends(current_user)):
    return await run_in_threadpool(service.list_runs, user["id"])


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.get_run, user["id"], run_id)


@router.get("/runs/{run_id}/events")
async def events(run_id: str, cursor: int = 0, latest: bool = False, user=Depends(current_user)):
    return await run_in_threadpool(
        service.get_events, user["id"], run_id, max(0, cursor), latest=latest
    )


@router.get("/runs/{run_id}/preview/{number}")
async def preview_page(run_id: str, number: int, fingerprint: str, user=Depends(current_user)):
    data = await run_in_threadpool(service.preview_page, user["id"], run_id, number, fingerprint)
    return Response(
        data,
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/runs/{run_id}/decisions", status_code=202)
async def decisions(run_id: str, body: DecisionBody, user=Depends(current_user)):
    return await run_in_threadpool(
        service.submit_decision, user["id"], run_id, body.pending_id, body.version, body.payload
    )


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.cancel, user["id"], run_id)


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry(run_id: str, user=Depends(current_user)):
    return await run_in_threadpool(service.retry, user["id"], run_id)


@router.get("/runs/{run_id}/download")
async def download(run_id: str, kind: str = "document", user=Depends(current_user)):
    if kind not in {"document", "report"}:
        raise service.ApiError(400, "未知的文件类型")
    data, name, mime = await run_in_threadpool(service.download, user["id"], run_id, kind)
    return Response(
        data,
        media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )
