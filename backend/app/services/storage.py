"""Object storage abstraction: local filesystem, or any S3-compatible store (AWS S3, MinIO)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, NotFound


class Storage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:  # blocks ../ traversal and absolute keys
            raise ValueError("Invalid storage key")
        return p

    async def put(self, key: str, data: bytes) -> None:
        p = self._path(key)

        def _w() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)

        await asyncio.to_thread(_w)

    async def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise NotFound("Stored object missing")
        return await asyncio.to_thread(p.read_bytes)

    async def delete(self, key: str) -> None:
        p = self._path(key)
        await asyncio.to_thread(lambda: p.unlink(missing_ok=True))


class S3Storage:
    """boto3 is synchronous, so every call runs in a worker thread. Keys are validated like LocalStorage (no traversal)."""

    def __init__(self, bucket: str, *, endpoint: str | None, access_key: str | None, secret_key: str | None,
                 region: str = "us-east-1", create_bucket: bool = True) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._c = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                               region_name=region, config=Config(retries={"max_attempts": 3, "mode": "standard"},
                                                                  s3={"addressing_style": "path" if endpoint else "auto"}))
        if create_bucket:
            self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self._c.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                raise
            self._c.create_bucket(Bucket=self.bucket)

    @staticmethod
    def _key(key: str) -> str:
        if key.startswith("/") or ".." in key.split("/") or "\\" in key or not key:
            raise ValueError("Invalid storage key")
        return key

    async def put(self, key: str, data: bytes) -> None:
        k = self._key(key)
        await asyncio.to_thread(lambda: self._c.put_object(Bucket=self.bucket, Key=k, Body=data))

    async def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        k = self._key(key)

        def _g() -> bytes:
            try:
                return self._c.get_object(Bucket=self.bucket, Key=k)["Body"].read()
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                    raise NotFound("Stored object missing") from e
                raise

        return await asyncio.to_thread(_g)

    async def delete(self, key: str) -> None:
        k = self._key(key)
        await asyncio.to_thread(lambda: self._c.delete_object(Bucket=self.bucket, Key=k))


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_dir)
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise ConfigurationError("NEXUS_S3_BUCKET is required for the s3 storage backend")
        return S3Storage(settings.s3_bucket, endpoint=settings.s3_endpoint, access_key=settings.s3_access_key,
                         secret_key=settings.s3_secret_key, region=settings.s3_region)
    raise ConfigurationError(f"Unknown storage backend {settings.storage_backend!r}")
