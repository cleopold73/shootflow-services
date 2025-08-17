# Python
#!/usr/bin/env python3
import base64
import json
import os
import tempfile
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

import boto3
import flickrapi

from shootflow.shared.config import settings


class FlickrAPIClient:
    """
    Thin wrapper around flickrapi suitable for expansion.
    Credentials are loaded automatically from AWS Secrets Manager.

    settings.toml keys:
      - AWS_REGION: AWS region for Secrets Manager
      - FLICKR_SECRET_NAME: Name/ARN of the secret containing Flickr credentials

    Secret JSON structure:
      {
        "api_key": "...",
        "api_secret": "...",
        "oauth_token": "...",
        "oauth_token_secret": "..."
      }
    """

    def __init__(self, *, store_token: bool = False) -> None:
        secret_name = settings.FLICKR_SECRET_NAME
        if not secret_name:
            raise RuntimeError("FLICKR_SECRET_NAME is not configured in settings.toml")

        region = settings.AWS_REGION
        sm = boto3.client("secretsmanager", region_name=str(region))

        # Load secret (supports both SecretString and SecretBinary)
        resp = sm.get_secret_value(SecretId=str(secret_name))
        if "SecretString" in resp and resp["SecretString"]:
            raw = resp["SecretString"]
        else:
            raw = base64.b64decode(resp.get("SecretBinary", b"")).decode("utf-8")

        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            raise RuntimeError(f"Failed to parse Flickr secret JSON: {e}")

        api_key = str(data.get("api_key", "")).strip()
        api_secret = str(data.get("api_secret", "")).strip()
        oauth_token = str(data.get("oauth_token", "")).strip()
        oauth_token_secret = str(data.get("oauth_token_secret", "")).strip()

        if not all([api_key, api_secret, oauth_token, oauth_token_secret]):
            raise RuntimeError("Flickr secret missing required keys (api_key, api_secret, oauth_token, oauth_token_secret)")

        # Initialize flickrapi; upload returns XML regardless of format
        self._flickr = flickrapi.FlickrAPI(
            api_key,
            api_secret,
            format="parsed-json",
            store_token=store_token,
            cache=True,
        )

        # Inject access tokens so calls are signed without re-auth
        self._flickr.token_cache.token = {
            "oauth_token": oauth_token,
            "oauth_token_secret": oauth_token_secret,
        }

    @staticmethod
    def _tags_to_string(tags: Optional[Iterable[str]]) -> Optional[str]:
        if not tags:
            return None
        return " ".join(t for t in tags if t)

    @staticmethod
    def _extract_photo_id(upload_resp) -> str:
        """
        flickrapi.upload returns an XML Element like:
          <rsp stat="ok"><photoid>12345</photoid></rsp>
        """
        if upload_resp is None:
            raise RuntimeError("Empty response from Flickr upload")

        try:
            root = upload_resp if isinstance(upload_resp, ET.Element) else ET.fromstring(upload_resp)
        except Exception as e:
            raise RuntimeError(f"Unexpected upload response (not XML): {e}")

        stat = root.attrib.get("stat")
        if stat != "ok":
            err = root.find("err")
            if err is not None:
                code = err.attrib.get("code")
                msg = err.attrib.get("msg")
                raise RuntimeError(f"Flickr upload failed: code={code} msg={msg}")
            raise RuntimeError("Flickr upload failed with unknown error")

        pid = root.findtext("photoid")
        if not pid:
            raise RuntimeError("Upload succeeded but photoid missing in response")
        return pid.strip()

    @staticmethod
    def _to_flag(value) -> int:
        """
        Coerce a boolean-like value to 0/1 int accepted by Flickr API.
        """
        if isinstance(value, bool):
            return 1 if value else 0
        try:
            return 1 if int(value) else 0
        except Exception:
            return 0

    def verify_auth(self) -> dict:
        """
        Optional: simple auth sanity check (returns user info from flickr.test.login).
        Raises on error.
        """
        return self._flickr.test.login()

    def upload_file(
        self,
        file_path: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        is_public: bool | int = False,
        is_friend: bool | int = False,
        is_family: bool | int = False,
        safety_level: Optional[int] = None,  # 1=safe, 2=moderate, 3=restricted
        content_type: Optional[int] = None,  # 1=photo, 2=screenshot, 3=other
        hidden: Optional[int] = None,        # 1=hidden from public searches, 2=visible
    ) -> str:
        """
        Upload a file from disk. Returns the new Flickr photo_id.

        Notes:
          - is_public/is_friend/is_family accept bool or int values; they are coerced to 0/1.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        kwargs = {
            "filename": file_path,
            "title": title or "",
            "description": description or "",
            "tags": self._tags_to_string(tags),
            "is_public": self._to_flag(is_public),
            "is_friend": self._to_flag(is_friend),
            "is_family": self._to_flag(is_family),
        }
        if safety_level is not None:
            kwargs["safety_level"] = int(safety_level)
        if content_type is not None:
            kwargs["content_type"] = int(content_type)
        if hidden is not None:
            kwargs["hidden"] = int(hidden)

        resp = self._flickr.upload(**kwargs)
        return self._extract_photo_id(resp)

    def upload_bytes(
        self,
        data: bytes,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        is_public: bool | int = False,
        is_friend: bool | int = False,
        is_family: bool | int = False,
        safety_level: Optional[int] = None,
        content_type: Optional[int] = None,
        hidden: Optional[int] = None,
    ) -> str:
        """
        Upload from in-memory bytes by writing a temporary file.
        Returns the new Flickr photo_id.

        Notes:
          - is_public/is_friend/is_family accept bool or int values; they are coerced to 0/1.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")

        with tempfile.NamedTemporaryFile(prefix="flickr_upload_", suffix=".jpg", delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            tmp_path = tmp.name

        try:
            return self.upload_file(
                tmp_path,
                title=title,
                description=description,
                tags=tags,
                is_public=is_public,
                is_friend=is_friend,
                is_family=is_family,
                safety_level=safety_level,
                content_type=content_type,
                hidden=hidden,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
