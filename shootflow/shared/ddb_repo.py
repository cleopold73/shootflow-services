from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

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
    ) -> Tuple[bool, Optional[str]]:
        now_iso = datetime.now(tz=timezone.utc).isoformat()

        item = {
            "photoId": {"S": photoId},  # Primary (partition) key
            "status": {"S": self.status_ready},
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
            UpdateExpression="SET #st = :posted, posted_at = :ts, flickr_photo_id = :fid",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":posted": {"S": "POSTED"},
                ":ts": {"S": now_iso},
                ":fid": {"S": flickr_photo_id},
            },
            ConditionExpression="attribute_exists(photoId)",
        )

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
