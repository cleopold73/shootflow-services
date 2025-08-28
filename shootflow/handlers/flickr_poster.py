# Python
import logging
from typing import Any, Dict, List, Optional

from shootflow.shared.config import settings
from shootflow.shared.metrics import metrics
from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.shared.s3 import S3Storage
from shootflow.shared.logger import setup_basic_logging
from shootflow.flickr import FlickrAPIClient  # uses Secrets Manager under the hood
from shootflow.flickr import suggest_groups_via_llm


def _normalize_ids(values: Optional[List[Any]]) -> List[str]:
    out: List[str] = []
    if not values:
        return out
    for v in values:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _apply_group_overrides(initial: List[str]) -> List[str]:
    """
    Post-process a list of group IDs using configuration:
      - FLICKR_EXCLUDE_GROUP_IDS: remove these IDs if present
      - FLICKR_INCLUDE_GROUP_IDS: ensure these IDs are present (append if missing)
    Preserves the order of the initial list; includes are appended in their configured order.
    """
    exclude = set(_normalize_ids(getattr(settings, "FLICKR_EXCLUDE_GROUP_IDS", [])))
    include = _normalize_ids(getattr(settings, "FLICKR_INCLUDE_GROUP_IDS", []))

    # Filter excluded
    filtered: List[str] = [gid for gid in initial if gid not in exclude]

    # Append includes if not already present and not excluded
    seen = set(filtered)
    for gid in include:
        if gid not in seen and gid not in exclude:
            filtered.append(gid)
            seen.add(gid)
    return filtered

logger = logging.getLogger(__name__)

# Shared services
_repo = PhotoRepository()
_s3_store = S3Storage()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Picks one random READY item from DynamoDB, downloads the image from S3,
    uploads it to Flickr, then marks it as POSTED.
    """
    setup_basic_logging()
    logger.info("flickr_poster_start")

    client = FlickrAPIClient()

    item = _repo.get_random_ready_photo_via_gsi()
    if not item:
        logger.info("no_ready_items")
        return {"posted": 0, "errors": 0}

    try:
        photo_id: str = item["photoId"]
        s3_key: str = item["s3_key"]

        # Dry run: compute group suggestions and exit without uploading
        if settings.FLICKR_GROUPS_DRY_RUN_ENABLED:
            groups: List[str] = []
            try:
                static_ids = settings.FLICKR_STATIC_GROUP_IDS
                if static_ids:
                    groups = [str(g).strip() for g in static_ids if str(g).strip()]
                elif settings.FLICKR_GROUPS_VIA_LLM_ENABLED:
                    groups = suggest_groups_via_llm(item, flickr_client=client, settings=settings)
                else:
                    logger.info("flickr_groups_dry_run LLM disabled; no groups suggested")
                # Apply include/exclude overrides
                groups = _apply_group_overrides(groups)
                metrics.count("FlickrGroupsSuggested", float(len(groups)))
            except Exception as ge:
                logger.warning(f"group_selection_dry_run_failed photoId={photo_id} error={ge}", exc_info=True)

            logger.info(
                f"flickr_groups_dry_run photoId={photo_id} title={item.get('title')} "
                f"tags={item.get('tags')} groups={groups}"
            )
            return {"posted": 0, "errors": 0}

        bucket = settings.S3_BUCKET
        if not bucket:
            raise RuntimeError("S3_BUCKET is not configured")

        # Download from S3 via shared storage interface
        data = _s3_store.download_bytes(bucket, s3_key)

        # Upload to Flickr using the real API client
        flickr_id = client.upload_bytes(
            data,
            is_public=settings.FLICKR_UPLOAD_PUBLIC
            # You can tweak visibility/safety defaults here if desired
        )

        # Mark as posted in DynamoDB
        _repo.mark_posted(photo_id, flickr_photo_id=flickr_id)

        # Optionally pick Flickr groups for this photo and add it to the selected groups only if posting is public
        if settings.FLICKR_UPLOAD_PUBLIC:
            try:
                groups: List[str] = []
                static_ids = settings.FLICKR_STATIC_GROUP_IDS
                if static_ids:
                    groups = [str(g).strip() for g in static_ids if str(g).strip()]
                elif settings.FLICKR_GROUPS_VIA_LLM_ENABLED:
                    groups = suggest_groups_via_llm(item, flickr_client=client, settings=settings)

                # Apply include/exclude overrides
                groups = _apply_group_overrides(groups)

                if groups:
                    metrics.count("FlickrGroupsSuggested", float(len(groups)))
                    logger.info(f"flickr_groups_selected photoId={photo_id} groups={groups}")
                    # Attempt to add the newly uploaded photo to each selected group (best effort)
                    try:
                        added = 0
                        for gid in groups:
                            client.add_photo_to_group(flickr_id, gid)
                            added += 1
                        if added:
                            metrics.count("FlickrGroupsAdded", float(added))
                            logger.info(f"flickr_groups_added photoId={photo_id} added={added}")
                    except Exception as ae:
                        logger.warning(f"flickr_groups_add_failed photoId={photo_id} error={ae}", exc_info=True)
            except Exception as ge:
                # Do not fail the posting flow on group selection errors
                logger.warning(f"group_selection_failed photoId={photo_id} error={ge}", exc_info=True)

        metrics.count("FlickrPosted", 1.0)
        logger.info(f"flickr_post_succeeded photoId={photo_id} flickr_id={flickr_id} s3_key={s3_key}")
        return {"posted": 1, "errors": 0}
    except Exception as e:
        metrics.count("FlickrPostFailed", 1.0)
        logger.exception(f"flickr_post_failed item={item} error={e}")
        raise


if __name__ == "__main__":
     handler({}, None)
