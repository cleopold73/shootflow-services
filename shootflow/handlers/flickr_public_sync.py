# Python
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from shootflow.flickr import FlickrAPIClient
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.logger import setup_basic_logging

logger = logging.getLogger(__name__)


def _default_window_days(days: int = 365*5) -> tuple[datetime, datetime]:
    now = datetime.now(tz=timezone.utc)
    return now - timedelta(days=days), now


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Enumerate the authenticated user's public Flickr photos within a date range and
    upsert them into DynamoDB as POSTED items.

    Event fields (all optional):
      - start_date: ISO8601 string, epoch seconds, or datetime-parseable string (default: now-365d)
      - end_date: ISO8601 string, epoch seconds, or datetime-parseable string (default: now)
      - by: "uploaded" or "taken" (default: "uploaded")
      - per_page: int (default: 500)
    """
    setup_basic_logging()
    start_date: Optional[Any] = event.get("start_date")
    end_date: Optional[Any] = event.get("end_date")
    by: str = (event.get("by") or "uploaded").strip().lower()
    per_page: int = int(event.get("per_page") or 500)

    if start_date is None or end_date is None:
        start_dt, end_dt = _default_window_days(365*5)
        start_date = start_dt
        end_date = end_dt

    client = FlickrAPIClient()
    repo = PhotoRepository()

    inserted = 0
    duplicates = 0
    errors = 0

    for p in client.iter_public_photos_date_range(
        start_date=start_date, end_date=end_date, by=by, per_page=per_page
    ):
        try:
            # Namespaced primary key to avoid collision with other ingestion sources
            photo_id_key = f"flickr:{p.id}"
            # Minimal placeholders for required fields
            s3_key = f"N/A"
            filename = f"N/A"

            ok, err = repo.put_photo_item(
                photoId=photo_id_key,
                s3_key=s3_key,
                filename=filename,
                title=p.title,
                caption=p.description,
                tags=p.tags or [],
                gps=None,
                exif={},
                status="POSTED",
                posted_time=p.date_uploaded,
                flickr_id=p.id,
            )
            if ok:
                inserted += 1
            else:
                if err == "duplicate":
                    duplicates += 1
                else:
                    errors += 1
                    logger.warning("put_photo_item_failed id=%s error=%s", p.id, err)
        except Exception as e:
            errors += 1
            logger.exception("sync_photo_failed id=%s error=%s", getattr(p, "id", "?"), e)

    result = {
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "by": by,
        "per_page": per_page,
    }
    logger.info("flickr_public_sync_summary %s", result)
    return result

if __name__ == "__main__":
     handler({}, None)
