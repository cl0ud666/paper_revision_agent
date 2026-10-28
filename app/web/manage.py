"""部署初始化与邀请制账号管理：python -m app.web.manage --help。"""

import argparse
import getpass
import os
import secrets
import time

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.web.db import transaction
from app.web.models import User
from app.web.service import add_user, reset_password
from app.web.storage import store
from app.workflow.graph import setup_checkpointer


def init_env():
    target = PROJECT_ROOT / "deploy" / ".env"
    postgres, redis, minio, password = (secrets.token_urlsafe(24) for _ in range(4))
    values = {
        "POSTGRES_PASSWORD": postgres,
        "REDIS_PASSWORD": redis,
        "MINIO_ROOT_USER": "paper-admin",
        "MINIO_ROOT_PASSWORD": minio,
        "PRA_ADMIN_USERNAME": "admin",
        "PRA_ADMIN_PASSWORD": password,
        "PRA_PUBLIC_ORIGIN": "http://localhost:8080",
        "PRA_COOKIE_SECURE": "0",
        "PRA_WEB_DEMO": "0",
        "PRA_DEFAULT_DAILY_RUNS": "5",
        "PRA_DEFAULT_RUN_TOKENS": "400000",
        "PRA_FREE_RUNS": "3",
        "PRA_REGISTRATION_OPEN": "1",
        "PRA_WORKER_CONCURRENCY": "5",
        "PRA_LLM_CONCURRENCY": "15",
        "PRA_AUDIT_CONCURRENCY": "3",
    }
    with target.open("x", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    print(f"已生成 {target}；管理员账号和密码保存在此文件中。已有文件不会覆盖。")


def bootstrap():
    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    setup_checkpointer()
    storage = store()
    if hasattr(storage, "client"):
        from botocore.exceptions import ClientError

        for attempt in range(30):
            try:
                try:
                    storage.client.head_bucket(Bucket=storage.bucket)
                except ClientError as exc:
                    if str(exc.response["Error"]["Code"]) not in {"404", "NoSuchBucket"}:
                        raise
                    storage.client.create_bucket(Bucket=storage.bucket)
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(1)
    username = os.environ["PRA_ADMIN_USERNAME"].strip().lower()
    with transaction() as db:
        exists = db.scalar(select(User).where(User.username == username))
    if not exists:
        add_user(username, os.environ["PRA_ADMIN_PASSWORD"], admin=True)
    print("数据库、Checkpoint、文件桶和管理员账号已就绪。")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init-env")
    sub.add_parser("bootstrap")
    create = sub.add_parser("create-user")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--daily-runs", type=int, default=5)
    create.add_argument("--run-tokens", type=int, default=400000)
    reset = sub.add_parser("reset-password")
    reset.add_argument("username")
    args = parser.parse_args()
    if args.action == "init-env":
        init_env()
    elif args.action == "bootstrap":
        bootstrap()
    else:
        password = getpass.getpass("新密码（至少6位）：")
        if password != getpass.getpass("再次输入："):
            raise SystemExit("两次密码不一致")
        if args.action == "reset-password":
            reset_password(args.username, password)
            print("密码已更新，此账号的所有旧会话已撤销。")
        else:
            add_user(
                args.username,
                password,
                admin=args.admin,
                daily_runs=args.daily_runs,
                run_tokens=args.run_tokens,
            )
            print("账号已创建。")


if __name__ == "__main__":
    main()
