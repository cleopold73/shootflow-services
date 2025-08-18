# Python
import logging
from typing import Any, Dict, List, Optional

from shootflow.shared.config import settings
from shootflow.shared.metrics import metrics
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.s3 import S3Storage
from shootflow.shared.flickr import FlickrAPIClient  # uses Secrets Manager under the hood

logger = logging.getLogger(__name__)

# Shared services
_repo = PhotoRepository()
_s3_store = S3Storage()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Picks one random READY item from DynamoDB, downloads the image from S3,
    uploads it to Flickr, then marks it as POSTED.
    """
    logger.info("flickr_poster_start")

    item = _repo.get_random_ready_photo_via_gsi()
    if not item:
        logger.info("no_ready_items")
        return {"posted": 0, "errors": 0}

    try:
        photo_id: str = item["photoId"]
        s3_key: str = item["s3_key"]
        title: Optional[str] = item.get("title")
        caption: Optional[str] = item.get("caption")
        tags: List[str] = item.get("tags") if isinstance(item.get("tags"), list) else []

        bucket = settings.S3_BUCKET
        if not bucket:
            raise RuntimeError("S3_BUCKET is not configured")

        # Download from S3 via shared storage interface
        data = _s3_store.download_bytes(bucket, s3_key)

        # Upload to Flickr using the real API client
        flickr_id = FlickrAPIClient().upload_bytes(
            data,
            title=title,
            description=caption,
            tags=tags,
            # You can tweak visibility/safety defaults here if desired
        )

        # Mark as posted in DynamoDB
        _repo.mark_posted(photo_id, flickr_photo_id=flickr_id)

        metrics.count("FlickrPosted", 1.0)
        logger.info(f"flickr_post_succeeded photoId={photo_id} flickr_id={flickr_id} s3_key={s3_key}")
        return {"posted": 1, "errors": 0}
    except Exception as e:
        metrics.count("FlickrPostFailed", 1.0)
        logger.exception(f"flickr_post_failed item={item} error={e}")
        raise


if __name__ == "__main__":
     handler({}, None)
