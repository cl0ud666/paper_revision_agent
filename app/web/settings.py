import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import PROJECT_ROOT


@dataclass(frozen=True)
class WebSettings:
    database_url: str
    redis_url: str
    public_origin: str
    secure_cookie: bool
    session_days: int
    upload_bytes: int
    daily_runs: int
    run_tokens: int
    llm_concurrency: int
    storage_backend: str
    storage_root: Path
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str
    free_runs: int
    registration_open: bool


@lru_cache(maxsize=1)
def settings() -> WebSettings:
    return WebSettings(
        database_url=os.getenv(
            "PRA_WEB_DATABASE_URL",
            "postgresql+psycopg://paper_agent:paper_agent@localhost:5432/paper_agent",
        ),
        redis_url=os.getenv("PRA_REDIS_URL", "redis://localhost:6379/0"),
        public_origin=os.getenv("PRA_PUBLIC_ORIGIN", "http://localhost:8080").rstrip("/"),
        secure_cookie=os.getenv("PRA_COOKIE_SECURE", "0") == "1",
        session_days=int(os.getenv("PRA_SESSION_DAYS", "7")),
        upload_bytes=int(os.getenv("PRA_UPLOAD_MAX_BYTES", str(20 * 1024 * 1024))),
        daily_runs=int(os.getenv("PRA_DEFAULT_DAILY_RUNS", "5")),
        run_tokens=int(os.getenv("PRA_DEFAULT_RUN_TOKENS", "400000")),
        llm_concurrency=int(os.getenv("PRA_LLM_CONCURRENCY", "6")),
        storage_backend=os.getenv("PRA_STORAGE_BACKEND", "s3"),
        storage_root=Path(
            os.getenv("PRA_LOCAL_STORAGE_ROOT", str(PROJECT_ROOT / "data" / "web-storage"))
        ),
        s3_endpoint=os.getenv("PRA_S3_ENDPOINT", "http://localhost:9000"),
        s3_bucket=os.getenv("PRA_S3_BUCKET", "papers"),
        s3_access_key=os.getenv("PRA_S3_ACCESS_KEY", ""),
        s3_secret_key=os.getenv("PRA_S3_SECRET_KEY", ""),
        s3_region=os.getenv("PRA_S3_REGION", "us-east-1"),
        free_runs=max(0, int(os.getenv("PRA_FREE_RUNS", "3"))),
        registration_open=os.getenv("PRA_REGISTRATION_OPEN", "1") == "1",
    )
