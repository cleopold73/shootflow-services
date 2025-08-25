# Python
import json
import os
import sys
import time
import logging
from typing import Any, Dict, Optional, Tuple

from shootflow.shared.config import settings
from shootflow.shared.metrics import metrics
from shootflow.shared.exif import run_exiftool_json, parse_exif_fields
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.s3 import S3Storage

logger = logging.getLogger(__name__)

# Centralized DynamoDB access via repository
_repo = PhotoRepository()

# Centralized S3 access via shared interface
_s3_store = S3Storage()

STATUS_READY = settings.S3_INGEST_STATUS_READY
PARTITION_PREFIX = settings.S3_INGEST_PARTITION_PREFIX
SORT_KEY_METADATA = settings.S3_INGEST_SORT_KEY_METADATA
READY_PREFIX = settings.S3_INGEST_READY_PREFIX


def _compute_sha256_and_save_temp_streaming(bucket: str, key: str) -> Tuple[str, str]:
    # Delegates to shared S3 interface
    return _s3_store.stream_to_temp_and_sha256(bucket, key)


def _maybe_url_unquote(key: str) -> str:
    try:
        from urllib.parse import unquote_plus
        return unquote_plus(key)
    except Exception:
        return key


def _ingest_single_record(bucket: str, key: str) -> None:
    if not key.startswith(READY_PREFIX):
        logger.info(f"skip_non_ready_prefix bucket={bucket} key={key}")
        return

    started = time.time()
    tmp_path = None
    photoId: Optional[str] = None
    try:
        photoId, tmp_path = _compute_sha256_and_save_temp_streaming(bucket, key)
        filename = key.split("/")[-1]

        exif_meta = run_exiftool_json(tmp_path) or {}
        title, caption, tags, extras = parse_exif_fields(exif_meta, fallback_title=os.path.splitext(filename)[0])

        created, err = _repo.put_photo_item(  # UPDATED
            photoId=photoId,
            s3_key=key,
            filename=filename,
            title=title,
            caption=caption,
            tags=tags,
            gps=extras.get("gps"),
            exif=extras.get("exif", {}),
        )

        duration_ms = int((time.time() - started) * 1000)
        if created:
            logger.info(
                f"ingest_succeeded bucket={bucket} key={key} photoId={photoId} "
                f"title={title} tag_count={len(tags)} duration_ms={duration_ms}"
            )
            metrics.count("IngestSucceeded", 1.0)
        else:
            if err == "duplicate":
                logger.info(
                    f"ingest_duplicate bucket={bucket} key={key} photoId={photoId} duration_ms={duration_ms}"
                )
                metrics.count("IngestDuplicate", 1.0)
            else:
                logger.error(
                    f"ingest_failed_putitem bucket={bucket} key={key} photoId={photoId} "
                    f"error={err} duration_ms={duration_ms}"
                )
                metrics.count("IngestFailed", 1.0)
                raise RuntimeError(f"put_item_failed err={err}")
    except Exception as e:
        logger.exception(f"ingest_exception bucket={bucket} key={key} photoId={photoId} error={e}")
        metrics.count("IngestFailed", 1.0)
        raise
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    S3 event handler invoked via the generic router using adapter 'sqs_s3'.

    The generic adapter unwraps SQS and calls this handler with a standard S3
    event shape. We process each S3 record independently and return a simple
    summary. Partial-batch retry semantics are handled by the router, not here.
    """
    s3_records = event.get("Records", []) or []
    logger.info(f"s3_ingest_start s3_records={len(s3_records)}")

    processed = 0
    errors = 0

    for s3r in s3_records:
        try:
            s3 = s3r.get("s3", {})
            bucket = s3.get("bucket", {}).get("name")
            key = s3.get("object", {}).get("key")
            if not bucket or not key:
                logger.warning(f"malformed_s3_event_record record={s3r}")
                errors += 1
                continue
            key = _maybe_url_unquote(key)
            _ingest_single_record(bucket, key)
            processed += 1
        except Exception as e:
            errors += 1
            logger.exception(f"s3_record_processing_failed error={e} record={s3r}")

    logger.info(f"s3_ingest_done processed={processed} errors={errors}")
    return {"processed": processed, "errors": errors}


if __name__ == "__main__":
    # Quick local check:
    # python -m src.handlers.s3_ingest s3://<bucket>/ready/<file>
    import re

    if len(sys.argv) != 2 or not sys.argv[1].startswith("s3://"):
        print("Usage: python -m src.handlers.s3_ingest s3://<bucket>/ready/<file>", file=sys.stderr)
        sys.exit(2)

    m = re.match(r"s3://([^/]+)/(.+)", sys.argv[1])
    if not m:
        print("Invalid S3 URI", file=sys.stderr)
        sys.exit(2)
    bucket_arg, key_arg = m.group(1), m.group(2)

    fake_event = {"Records": [{"s3": {"bucket": {"name": bucket_arg}, "object": {"key": key_arg}}}]}
    result = handler(fake_event, None)
    print(json.dumps(result))