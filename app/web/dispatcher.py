"""独立 outbox 投递进程：Redis 断线期间仍持续扫描 PostgreSQL。"""
from datetime import timedelta
import logging
import time

from sqlalchemy import select, or_, and_
from app.web.db import transaction
from app.web.models import Job, now
from app.web.queue import celery_app


def dispatch_once(publish=None):
    publish = publish or (lambda job_id: celery_app.send_task("paper.execute", args=[job_id], task_id=job_id, retry=False))
    count = 0
    with transaction() as db:
        stamp = now()
        jobs = db.scalars(select(Job).where(
            or_(and_(Job.status.in_(["pending", "published"]), Job.publish_after <= stamp),
                and_(Job.status == "running", Job.lease_until < stamp))
        ).order_by(Job.created_at).limit(20).with_for_update(skip_locked=True)).all()
        for job in jobs:
            try:
                publish(job.id)
            except Exception:
                # 失败保留原状态，独立进程稍后补发；不把 Broker 错误当业务失败。
                job.publish_after = stamp + timedelta(seconds=10)
                continue
            if job.status != "running":
                job.status = "published"
            else:
                job.lease_until = stamp + timedelta(seconds=60)
            job.publish_after = stamp + timedelta(seconds=60)
            count += 1
    return count


def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            count = dispatch_once()
            if count:
                logging.info("Published %s paper jobs", count)
        except Exception:
            logging.exception("Outbox dispatch failed; retrying")
        time.sleep(2)


if __name__ == "__main__":
    main()
