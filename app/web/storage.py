from functools import lru_cache

import boto3
from botocore.config import Config

from app.document.word_formats import validate_word
from app.web.settings import settings

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def validate_word_upload(data: bytes, filename: str) -> str:
    return validate_word(data, filename, max_bytes=settings().upload_bytes)[0]


class LocalStore:
    """仅用于测试或单机开发，生产 Compose 使用 S3/MinIO。"""

    def path(self, key: str):
        root = settings().storage_root.resolve()
        target = (root / key).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("非法存储路径")
        return target

    def put(self, key: str, data: bytes, content_type: str):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def delete(self, key: str):
        self.path(key).unlink(missing_ok=True)


class S3Store:
    def __init__(self):
        cfg = settings()
        self.bucket = cfg.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.s3_endpoint,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            region_name=cfg.s3_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2},
            ),
        )

    def put(self, key: str, data: bytes, content_type: str):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)


@lru_cache(maxsize=1)
def store():
    backend = settings().storage_backend
    if backend == "local":
        return LocalStore()
    if backend != "s3":
        raise ValueError("PRA_STORAGE_BACKEND 必须为 s3 或 local")
    return S3Store()
