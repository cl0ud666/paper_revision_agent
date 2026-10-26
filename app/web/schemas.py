"""Request validation for HTTP endpoints."""

from typing import Any

from pydantic import BaseModel, Field


class LoginBody(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class RegisterBody(LoginBody):
    password: str = Field(min_length=6, max_length=128)


class DecisionBody(BaseModel):
    pending_id: str = Field(min_length=36, max_length=36)
    version: int = Field(ge=1)
    payload: dict[str, Any]


class NewUserBody(RegisterBody):
    daily_run_limit: int = Field(default=5, ge=1, le=1000)
    run_token_limit: int = Field(default=400000, ge=1000, le=5000000)


class UserUpdateBody(BaseModel):
    active: bool | None = None
    daily_run_limit: int | None = Field(default=None, ge=1, le=1000)
    run_token_limit: int | None = Field(default=None, ge=1000, le=5000000)
