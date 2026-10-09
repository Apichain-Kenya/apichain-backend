"""The object-storage boundary (P3b-D, 04 §5.9 boundary 2, 11 §7).

**The only module on the media path that touches the network for bytes.**
MinIO in dev, DigitalOcean Spaces or S3 in production (04 §3.4): one S3 API,
so one adapter. Tests use `tests.fakes.FakeObjectStore` and reach nothing.

Keys are content-addressed (`sha256/<hex>`), so `put` is idempotent by
construction: an object that already exists under its own hash already holds
exactly these bytes, and is not written again.

**Signed URLs are signed for the public endpoint.** A presigned URL binds the
host it was signed for. Signed for the compose-internal `minio:9000`, it is
useless to a browser, and rewriting the host afterwards breaks the signature.
So the adapter keeps a second client configured with `s3_public_endpoint`,
used only to sign. Signing is local computation; the region is fixed so the
client never makes a network call to discover it.

**The signed URL forces `Content-Type` and `Content-Disposition: attachment`.**
The type is the one the bytes sniffed as, so a file that sniffed as PNG is
served as PNG and downloaded rather than rendered inline: bytes that also
parse as HTML can never run in the bucket's origin.

No retry here, as in `ots.py`: a failure raises `StorageUnavailable` and the
caller answers 503 having written nothing.
"""

import datetime as dt
import io
import logging
from typing import Protocol
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger("apichain.storage")


class StorageUnavailable(RuntimeError):
    """The object store could not be reached or refused the operation."""


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def presign_get(
        self, key: str, *, ttl: dt.timedelta, content_type: str, filename: str
    ) -> str: ...


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header with an RFC 5987 UTF-8 filename."""
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


class S3ObjectStore:
    """MinIO / S3 / Spaces via the `minio` SDK."""

    _REGION = "us-east-1"

    def __init__(
        self,
        *,
        endpoint: str,
        public_endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        secure: bool,
    ) -> None:
        from minio import Minio

        self._bucket = bucket
        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=self._REGION,
        )
        self._signer = Minio(
            public_endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=self._REGION,
        )
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
        self._bucket_ready = True

    def put(self, key: str, data: bytes, content_type: str) -> None:
        from minio.error import S3Error

        try:
            self._ensure_bucket()
            try:
                self._client.stat_object(self._bucket, key)
                return  # content-addressed: already holds these bytes
            except S3Error as exc:
                if exc.code not in ("NoSuchKey", "NoSuchObject"):
                    raise
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), len(data), content_type=content_type
            )
        except Exception as exc:  # any transport or service failure
            logger.warning("object store put failed: %s", type(exc).__name__)
            raise StorageUnavailable("object store unavailable") from exc

    def presign_get(self, key: str, *, ttl: dt.timedelta, content_type: str, filename: str) -> str:
        try:
            return self._signer.presigned_get_object(
                self._bucket,
                key,
                expires=ttl,
                response_headers={
                    "response-content-type": content_type,
                    "response-content-disposition": content_disposition(filename),
                },
            )
        except Exception as exc:
            raise StorageUnavailable("could not sign a URL") from exc


def from_settings() -> ObjectStore:
    if settings.storage_backend == "fake":
        # Imported lazily: the in-memory store is a dev convenience and must
        # never be what a production process silently gets (11 D4).
        from app.services.dev_fakes import MemoryObjectStore

        return MemoryObjectStore.shared()
    return S3ObjectStore(
        endpoint=settings.s3_endpoint,
        public_endpoint=settings.s3_public_endpoint,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
