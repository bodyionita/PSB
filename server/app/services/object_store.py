"""Object storage seam for the R2 backups (ADR-014 §1, §7).

The durability jobs depend on the :class:`ObjectStore` *protocol*, not on boto3, so they are
unit-testable with an in-memory fake (no network in CI — 08 testing policy). :class:`R2ObjectStore`
is the Cloudflare-R2 (S3-compatible) implementation; boto3 is **imported lazily** inside it so the
module graph — and tests — never pull boto3 unless a real store is actually constructed.

``build_object_store`` returns ``None`` when R2 credentials are absent (dev), which the jobs treat
as "backups disabled" and skip cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ..config import Settings

logger = logging.getLogger(__name__)


class ObjectStore(Protocol):
    """The object-storage surface the durability jobs rely on."""

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class R2ObjectStore:
    """Cloudflare R2 via the S3 API (boto3). Blocking SDK calls run in worker threads (rule 8)."""

    def __init__(
        self, *, endpoint_url: str, access_key_id: str, secret_access_key: str, bucket: str
    ) -> None:
        import boto3  # lazy: only a real R2 store needs the SDK

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",  # R2 ignores region but the SDK requires one
        )

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def get_bytes(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()

        return await asyncio.to_thread(_get)

    async def list_keys(self, prefix: str) -> list[str]:
        def _list() -> list[str]:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            return keys

        return await asyncio.to_thread(_list)


def _endpoint_url(settings: Settings) -> str:
    if settings.r2_endpoint_url:
        return settings.r2_endpoint_url
    return f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"


def build_object_store(settings: Settings) -> ObjectStore | None:
    """Construct the R2 store, or ``None`` when credentials are absent (backups disabled)."""
    if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key):
        logger.info("R2 credentials not set — vault/db/data backups to object storage disabled")
        return None
    return R2ObjectStore(
        endpoint_url=_endpoint_url(settings),
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
        bucket=settings.r2_bucket,
    )
