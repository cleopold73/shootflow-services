# Python
from __future__ import annotations

import hashlib
import io
import tempfile
from typing import Tuple

import boto3

from shootflow.shared.config import settings


class S3Storage:
    """
    Thin S3 access layer for streaming and downloads used by handlers.

    Responsibilities:
      - Stream an object to a temp file while computing SHA-256
      - Download entire object into memory (when needed)
    """

    def __init__(self) -> None:
        region_name = settings.AWS_REGION
        self._s3 = boto3.client("s3", region_name=region_name)

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
