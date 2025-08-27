# Python
from __future__ import annotations

import hashlib
import io
import mimetypes
import tempfile
from typing import Iterator, Tuple

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

from shootflow.shared.config import settings


class S3Storage:
    """
    Thin S3 access layer for streaming, downloads, and uploads used by handlers and tools.

    Responsibilities:
      - Stream an object to a temp file while computing SHA-256
      - Download entire object into memory (when needed)
      - Upload helpers (file/bytes) with sensible defaults
      - Key helpers for the ready/ prefix used by ingest
    """

    def __init__(self) -> None:
        region_name = settings.AWS_REGION
        # Configure retries/timeouts a bit safer than defaults
        cfg = Config(
            region_name=region_name,
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=10,
            read_timeout=120,
        )
        self._s3 = boto3.client("s3", config=cfg)
        self._tx = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            multipart_chunksize=8 * 1024 * 1024,
            max_concurrency=4,
            use_threads=True,
        )

    def stream_to_temp_and_sha256(self, bucket: str, key: str) -> Tuple[str, str]:
        """
        Streams object content from S3 to a temporary file while computing a SHA-256.
        Returns (hex_digest, temp_file_path). Caller is responsible for deleting the temp file.
        """
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]

        hasher = hashlib.sha256()
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp_path = tmp.name
        with tmp as f:
            while True:
                chunk = body.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                hasher.update(chunk)
                f.write(chunk)

        return hasher.hexdigest(), tmp_path

    def download_bytes(self, bucket: str, key: str) -> bytes:
        """
        Downloads an entire object into memory.
        """
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        buf = io.BytesIO()
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            buf.write(chunk)
        return buf.getvalue()

    # =============== Upload and utility helpers ===============
    def build_ready_key(self, filename: str) -> str:
        name = filename.rsplit("/", 1)[-1]
        safe = name.replace(" ", "_")
        prefix = settings.S3_INGEST_READY_PREFIX or "ready/"
        if not prefix.endswith("/"):
            prefix = prefix + "/"
        return f"{prefix}{safe}"

    def upload_file(
        self,
        path: str,
        bucket: str,
        key: str,
        *,
        content_type: str | None = None,
        sse: str | None = None,
    ) -> None:
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        extra_args = {"ContentType": ctype}
        if sse:
            extra_args["ServerSideEncryption"] = sse  # e.g., "AES256"
        self._s3.upload_file(path, bucket, key, ExtraArgs=extra_args, Config=self._tx)

    def upload_bytes(
        self,
        data: bytes,
        bucket: str,
        key: str,
        *,
        content_type: str | None = None,
        sse: str | None = None,
    ) -> None:
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        extra_args = {"ContentType": ctype}
        if sse:
            extra_args["ServerSideEncryption"] = sse
        self._s3.put_object(Bucket=bucket, Key=key, Body=data, **extra_args)

    def upload_to_ready(self, path: str, bucket: str, *, key_name: str | None = None) -> str:
        from pathlib import Path as _P
        filename = key_name or _P(path).name
        key = self.build_ready_key(filename)
        self.upload_file(path, bucket, key)
        return key

    def object_exists(self, bucket: str, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            # ClientError includes 404; treat any failure as not-exists for this helper
            return False

    def delete_object(self, bucket: str, key: str) -> None:
        self._s3.delete_object(Bucket=bucket, Key=key)

    def list_ready(self, bucket: str, prefix: str | None = None) -> Iterator[str]:
        pf = prefix or (settings.S3_INGEST_READY_PREFIX or "ready/")
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=pf):
            for obj in page.get("Contents", []) or []:
                yield obj["Key"]

    def generate_presigned_put_url(self, bucket: str, key: str, *, expires_in: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(expires_in),
        )
