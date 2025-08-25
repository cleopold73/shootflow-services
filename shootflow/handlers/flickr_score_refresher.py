# Python
import logging
from typing import Any, Dict, Optional

from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.flickr import FlickrAPIClient
from shootflow.shared.config import settings

logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Iterate all (photoId, flickr_photo_id) from DynamoDB, compute an updated Flickr score
    for each photo, and write it back to the table.

    Optional event fields:
      - page_limit: int page size for the DynamoDB Scan iterator (default: 1000)
      - max_items: int cap on total processed items (default: no cap)
      - dry_run: bool; if True, compute scores but do not write updates (default: False)
    """
    page_limit: int = int(event.get("page_limit", 1000) or 1000)
    max_items_raw: Optional[int] = event.get("max_items")
    max_items: Optional[int] = int(max_items_raw) if max_items_raw is not None else None
    dry_run: bool = settings.FLICKR_SCORE_REFRESH_DRY_RUN

    repo = PhotoRepository()
    flickr = FlickrAPIClient()

    updated = 0
    errors = 0
    processed = 0

    for photo_id, flickr_id in repo.iter_all_flickr_photo_ids(page_limit=page_limit):
        if max_items is not None and processed >= max_items:
            break

        try:
            score = flickr.get_photo_score(flickr_id)
            if not dry_run:
                repo.update_flickr_score(photo_id, score)
            updated += 1
        except Exception as e:
            errors += 1
            logger.warning("flickr_score_update_failed photoId=%s flickr_id=%s error=%s", photo_id, flickr_id, e, exc_info=False)

        processed += 1
        if processed % 100 == 0:
            logger.info("flickr_score_progress processed=%d updated=%d errors=%d", processed, updated, errors)

    result = {
        "processed": processed,
        "updated": updated,
        "errors": errors,
        "dry_run": dry_run,
        "page_limit": page_limit,
        "max_items": max_items,
    }
    logger.info("flickr_score_refresher_summary %s", result)
    return result

if __name__ == "__main__":
     handler({}, None)
