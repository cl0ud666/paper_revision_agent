"""Isolated PostgreSQL concurrency regression, run inside the API image.

Uses a random temporary schema and local temp storage. No production accounts,
papers, Celery jobs or model calls. Drops ONLY its validated temporary schema.
"""

import json
import os
import re
import secrets
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url


def main():
    sys.path.insert(0, str(Path.cwd()))
    url = make_url(os.environ["PRA_WEB_DATABASE_URL"])
    assert url.drivername.startswith("postgresql"), "This verification needs PostgreSQL"
    schema = "verify_trial_" + uuid4().hex
    assert re.fullmatch(r"verify_trial_[a-f0-9]{32}", schema)
    admin_engine = create_engine(url)
    with admin_engine.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        with tempfile.TemporaryDirectory(prefix="pra-five-users-") as directory:
            os.environ.update(
                PRA_WEB_DATABASE_URL=url.update_query_dict(
                    {"options": f"-csearch_path={schema}"}
                ).render_as_string(hide_password=False),
                PRA_STORAGE_BACKEND="local",
                PRA_LOCAL_STORAGE_ROOT=directory,
                PRA_FREE_RUNS="3",
                PRA_DEFAULT_DAILY_RUNS="20",
            )
            from app.web.db import engine, reset_engines, transaction
            from app.web.settings import settings
            from app.web.storage import store

            settings.cache_clear()
            reset_engines()
            store.cache_clear()
            from app.web import service
            from app.web.models import Base, Job
            from app.web.worker import execute_job
            from app.workflow.harness import RunResult

            Base.metadata.create_all(engine())
            from docx import Document

            doc = Document()
            doc.add_paragraph("concurrency verification")
            buffer = BytesIO()
            doc.save(buffer)
            data = buffer.getvalue()
            users = [
                service.add_user(f"verify{i}", secrets.token_urlsafe(24))["id"] for i in range(6)
            ]
            barrier = Barrier(5)

            def upload(uid):
                barrier.wait(timeout=20)
                try:
                    return service.create_run(uid, data, "test.docx", "Review", [])
                except service.ApiError as exc:
                    return exc.status

            with ThreadPoolExecutor(max_workers=5) as pool:
                same_user = list(pool.map(upload, [users[0]] * 5))
            assert sum(isinstance(r, dict) for r in same_user) == 3, same_user
            assert same_user.count(403) == 2, same_user
            barrier = Barrier(5)
            with ThreadPoolExecutor(max_workers=5) as pool:
                five = list(pool.map(upload, users[1:]))
            assert all(isinstance(r, dict) for r in five)
            with transaction() as db:
                jobs = [db.scalar(select(Job.id).where(Job.run_id == r["id"])) for r in five]
            running = Barrier(5)

            def drive(run, job, harness):
                # If work is accidentally serialized, this barrier times out.
                running.wait(timeout=30)
                return RunResult(
                    run.id,
                    f"web-{run.id}",
                    "interrupted",
                    state={},
                    interrupts=[{"type": "issue_review", "issues": [], "owner": run.user_id}],
                )

            with patch("app.web.worker.drive_workflow", side_effect=drive):
                with ThreadPoolExecutor(max_workers=5) as pool:
                    list(
                        pool.map(
                            lambda job: execute_job(
                                job, harness_factory=lambda **kw: SimpleNamespace()
                            ),
                            jobs,
                        )
                    )
            for uid, row in zip(users[1:], five):
                detail = service.get_run(uid, row["id"])
                assert detail["status"] == "waiting_user", detail
                assert detail["pending"]["request"]["owner"] == uid
                try:
                    service.get_run(users[0], row["id"])
                    raise AssertionError("Cross-user read permitted")
                except service.ApiError as exc:
                    assert exc.status == 404
            # Real Redis Lua limit, isolated random keys, no actual login/SMS.
            from app.web.queue import redis_client, registration_rate_limit

            redis = redis_client()
            keys = []

            class IsolatedRedis:
                def eval(self, script, number, key, *args):
                    isolated = schema + ":" + key
                    keys.append(isolated)
                    return redis.eval(script, number, isolated, *args)

            try:
                with patch("app.web.queue.redis_client", return_value=IsolatedRedis()):
                    for _ in range(5):
                        registration_rate_limit("203.0.113.1")
                    try:
                        registration_rate_limit("203.0.113.1")
                        raise AssertionError("Registration limit bypassed")
                    except service.ApiError as exc:
                        assert exc.status == 429
            finally:
                if keys:
                    redis.delete(*set(keys))
            print(
                json.dumps(
                    {
                        "five_parallel_user_workers": "passed",
                        "cross_user_isolation": "passed",
                        "same_user_five_submissions_only_three_admitted": "passed",
                        "redis_registration_limit": "passed",
                    }
                )
            )
            reset_engines()
    finally:
        with admin_engine.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


if __name__ == "__main__":
    main()
