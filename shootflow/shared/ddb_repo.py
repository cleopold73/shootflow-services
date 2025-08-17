from __future__ import annotations

import json
import logging
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

    def __init__(self, table_name: str, region_name: str, ddb_client=None):
        self.table_name = table_name
        self.ddb = ddb_client or boto3.client("dynamodb", region_name=region_name)

        # Config from settings
        self.status_ready = settings.S3_INGEST_STATUS_READY

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
