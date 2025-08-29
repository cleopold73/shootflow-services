from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, Iterator

import boto3
from botocore.exceptions import ClientError

from shootflow.shared.config import settings

logger = logging.getLogger(__name__)


class PhotoRepository:
    """
    Encapsulates all DynamoDB access for photo metadata.
    Table must have:
      - Partition key: photoId (String)
      - No sort key
    """

    def __init__(self, ddb_client=None):
        self.table_name = settings.DDB_TABLE
        region_name = settings.AWS_REGION
        self.ddb = ddb_client or boto3.client("dynamodb", region_name=region_name)

        # Config from settings
        self.status_ready = settings.S3_INGEST_STATUS_READY
        # Name of the GSI: partition key = status, sort key = photoId
        self.status_gsi_name = "status-photoId-gsi"

    def put_photo_item(
        self,
        photoId: str,
        s3_key: str,
        filename: str,
        title: Optional[str],
        caption: Optional[str],
        tags: list[str],
        gps: Optional[Dict[str, Any]],
        exif: Dict[str, Any],
        *,
        status: Optional[str] = None,
        posted_time: Optional[datetime] = None,
        flickr_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        # Status: default to READY, allow explicit POSTED via 'posted=True'
        status_value = status if status else self.status_ready

        item = {
            "photoId": {"S": photoId},  # Primary (partition) key
            "status": {"S": status_value},
            "s3_key": {"S": s3_key},
            "filename": {"S": filename},
            "ingested_at": {"S": now_iso},
            "exif": {"S": json.dumps(exif)},
        }
        if title:
            item["title"] = {"S": title}
        if caption:
            item["caption"] = {"S": caption}
        if tags:
            item["tags"] = {"L": [{"S": t} for t in tags]}
        if gps is not None:
            item["gps"] = {"S": json.dumps(gps)}
        # Optional posted metadata
        if posted_time is not None:
            if isinstance(posted_time, datetime):
                pt_iso = posted_time.astimezone(timezone.utc).isoformat()
            else:
                pt_iso = str(posted_time)
            item["posted_at"] = {"S": pt_iso}
        if flickr_id:
            item["flickr_photo_id"] = {"S": flickr_id}

        try:
            self.ddb.put_item(
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(photoId)",
            )
            return True, None
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False, "duplicate"
            return False, str(e)

    def get_random_ready_photo_via_gsi(self) -> Optional[Dict[str, Any]]:
        """
        Select a single READY item using the GSI (status, photoId) with an unbiased approach:
          1) token = random 128-bit hex
          2) Query: status='READY' AND photoId >= token LIMIT 1
          3) If none, wrap-around: status='READY' AND photoId >= '0' LIMIT 1
        """
        token = secrets.token_hex(16)  # 128-bit hex
        index = self.status_gsi_name

        try:
            # First window: >= token
            resp = self.ddb.query(
                TableName=self.table_name,
                IndexName=index,
                Limit=1,
                KeyConditionExpression="#st = :ready AND photoId >= :tok",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":ready": {"S": self.status_ready}, ":tok": {"S": token}},
                ScanIndexForward=True,
            )
            items = resp.get("Items", [])
            if items:
                return self._deserialize_item(items[0])

            # Wrap-around window: from start
            resp2 = self.ddb.query(
                TableName=self.table_name,
                IndexName=index,
                Limit=1,
                KeyConditionExpression="#st = :ready AND photoId >= :min",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":ready": {"S": self.status_ready}, ":min": {"S": "0"}},
                ScanIndexForward=True,
            )
            items2 = resp2.get("Items", [])
            if items2:
                return self._deserialize_item(items2[0])

            return None
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            # If the index is missing/misconfigured, bubble up for visibility
            logger.error("GSI query failed (%s): %s", code, e, exc_info=True)
            raise

    def mark_posted(self, photoId: str, *, flickr_photo_id: str) -> None:
        """
        Transition item to POSTED, recording timestamp and Flickr photo id.
        """
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        self.ddb.update_item(
            TableName=self.table_name,
            Key={"photoId": {"S": photoId}},
            UpdateExpression="SET #st = :posted, posted_at = :posted_time, flickr_photo_id = :flickr_id",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":posted": {"S": "POSTED"},
                ":posted_time": {"S": now_iso},
                ":flickr_id": {"S": flickr_photo_id},
            },
            ConditionExpression="attribute_exists(photoId)",
        )

    def update_flickr_score(self, photoId: str, flickr_score: float) -> None:
        score_num = float(flickr_score)
        self.ddb.update_item(
            TableName=self.table_name,
            Key={"photoId": {"S": photoId}},
            UpdateExpression="SET flickr_score = :flickr_score",
            ExpressionAttributeValues={
                ":flickr_score": {"N": str(score_num)},
            },
            ConditionExpression="attribute_exists(photoId)",
        )

    def update_instagram_posted(self, photoId: str, instagram_posted: str) -> None:
        self.ddb.update_item(
            TableName=self.table_name,
            Key={"photoId": {"S": photoId}},
            UpdateExpression="SET instagram_posted = :instagram_posted",
            ExpressionAttributeValues={
                ":instagram_posted": {"S": str(instagram_posted)},
            },
            ConditionExpression="attribute_exists(photoId)",
        )

    def iter_all_flickr_photo_ids(self, *, page_limit: int = 1000) -> Iterator[Tuple[str, str]]:
        """
        Iterate over all items yielding (photoId, flickr_photo_id) tuples.
        Uses a paginated Scan with a projection to minimize IO.
        """
        exclusive_start_key: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {
                "TableName": self.table_name,
                "ProjectionExpression": "photoId, flickr_photo_id",
                "Limit": int(page_limit),
            }
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key

            resp = self.ddb.scan(**kwargs)
            for it in resp.get("Items", []):
                pid = it.get("photoId")
                fid = it.get("flickr_photo_id")
                if (
                    isinstance(pid, dict) and "S" in pid and pid["S"]
                    and isinstance(fid, dict) and "S" in fid and fid["S"]
                ):
                    yield pid["S"], fid["S"]

            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

    def get_photo_with_highest_flickr_score(self, *, page_limit: int = 1000) -> Optional[Tuple[str, str]]:
        """
        Scan the table and return (photoId, flickr_photo_id) for the item with the highest flickr_score.
        Excludes items where instagram_posted == "POSTED". If the field is missing or has any other value, the item is eligible.
        Returns None if no eligible items have a score and required attributes.
        """
        best_pid: Optional[str] = None
        best_fid: Optional[str] = None
        best_score: Optional[float] = None

        exclusive_start_key: Optional[Dict[str, Any]] = None
        while True:
            kwargs: Dict[str, Any] = {
                "TableName": self.table_name,
                "ProjectionExpression": "photoId, flickr_photo_id, flickr_score, instagram_posted",
                "Limit": int(page_limit),
            }
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key

            resp = self.ddb.scan(**kwargs)
            for it in resp.get("Items", []):
                # Skip if instagram_posted is explicitly "POSTED"
                ip = it.get("instagram_posted")
                if isinstance(ip, dict) and ip.get("S") == "POSTED":
                    continue

                pid = it.get("photoId")
                fid = it.get("flickr_photo_id")
                score_attr = it.get("flickr_score")
                if (
                    isinstance(pid, dict) and "S" in pid and pid["S"]
                    and isinstance(fid, dict) and "S" in fid and fid["S"]
                    and isinstance(score_attr, dict) and "N" in score_attr
                ):
                    try:
                        score_val = float(score_attr["N"])
                    except Exception:
                        continue
                    if best_score is None or score_val > best_score:
                        best_score = score_val
                        best_pid = pid["S"]
                        best_fid = fid["S"]

            exclusive_start_key = resp.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break

        if best_pid and best_fid:
            return best_pid, best_fid
        return None

    def get_item(self, photoId: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a single item by photoId and deserialize it into a plain dict.
        Returns None if the item does not exist.
        """
        try:
            resp = self.ddb.get_item(TableName=self.table_name, Key={"photoId": {"S": photoId}})
            item = resp.get("Item")
            if not item:
                return None
            return self._deserialize_item(item)
        except ClientError as e:
            logger.error("get_item failed for %s: %s", photoId, e, exc_info=True)
            raise

    @staticmethod
    def _deserialize_item(item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Minimal DDB attribute deserializer: converts a subset of DynamoDB types to Python.
        """
        out: Dict[str, Any] = {}
        for k, v in item.items():
            if "S" in v:
                out[k] = v["S"]
            elif "N" in v:
                out[k] = float(v["N"]) if "." in v["N"] else int(v["N"])
            elif "L" in v:
                out[k] = [x.get("S") or x.get("N") for x in v["L"]]
            elif "M" in v:
                out[k] = {ik: (iv.get("S") or iv.get("N")) for ik, iv in v["M"].items()}
            else:
                out[k] = v
        return out
