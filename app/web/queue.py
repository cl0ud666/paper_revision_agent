import hashlib
import secrets
import time
from functools import lru_cache

from celery import Celery
from redis import Redis

from app.web.settings import settings


@lru_cache(maxsize=1)
def redis_client():
    return Redis.from_url(
        settings().redis_url, socket_connect_timeout=5, socket_timeout=5, decode_responses=True
    )


celery_app = Celery("paper_revision", broker=settings().redis_url, include=["app.web.worker"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=1800,
    task_time_limit=2100,
    broker_transport_options={"visibility_timeout": 2400},
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
    task_default_queue="papers",
    worker_max_tasks_per_child=20,
)


def login_rate_limit(ip: str, username: str):
    # Match login's canonical username so surrounding whitespace cannot reset
    # the counter for the same account.
    key = "paper:login:" + hashlib.sha256(f"{ip}:{username.strip().lower()}".encode()).hexdigest()
    count = redis_client().eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
        1,
        key,
    )
    if count > 10:
        from app.web.service import ApiError

        raise ApiError(429, "登录尝试过于频繁，请稍后再试")


def registration_rate_limit(ip: str):
    # IP-only, not username: changing the account name must not bypass limits.
    key = "paper:register:" + hashlib.sha256(ip.encode()).hexdigest()
    count = redis_client().eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],3600) end; return n",
        1,
        key,
    )
    if count > 5:
        from app.web.service import ApiError

        raise ApiError(429, "该网络注册尝试过于频繁，请一小时后再试")


_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('EXPIRE', KEYS[1], 600)
return 1
"""


def acquire_llm_slot():
    token = secrets.token_hex(16)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        stamp = time.time()
        if redis_client().eval(
            _ACQUIRE, 1, "paper:llm:slots", stamp, stamp + 600, settings().llm_concurrency, token
        ):
            return token
        time.sleep(0.2)
    raise RuntimeError("模型并发名额暂时不足，请稍后重试")


def release_llm_slot(token):
    redis_client().zrem("paper:llm:slots", token)
