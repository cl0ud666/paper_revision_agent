from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.web import service
from app.web.dependencies import admin
from app.web.schemas import NewUserBody, UserUpdateBody

router = APIRouter()


@router.get("/admin/users")
async def users(user=Depends(admin)):
    return await run_in_threadpool(service.users)


@router.post("/admin/users", status_code=201)
async def create_user(body: NewUserBody, user=Depends(admin)):
    return await run_in_threadpool(
        service.add_user,
        body.username,
        body.password,
        daily_runs=body.daily_run_limit,
        run_tokens=body.run_token_limit,
    )


@router.patch("/admin/users/{user_id}")
async def update_user(user_id: str, body: UserUpdateBody, user=Depends(admin)):
    return await run_in_threadpool(service.update_user, user_id, body.model_dump(exclude_none=True))
