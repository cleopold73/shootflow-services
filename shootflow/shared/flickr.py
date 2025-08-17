#!/usr/bin/env python3
# python
import base64
import json
import os
import tempfile
from typing import Iterable, Optional
import logging

import boto3
import flickr_api

from shootflow.shared.config import settings

logger = logging.getLogger(__name__)


class FlickrAPIClient:
    """
    Thin wrapper around flickr_api suitable for expansion.
    Credentials are loaded automatically from AWS Secrets Manager.

    settings.toml keys:
      - AWS_REGION: AWS region for Secrets Manager
      - FLICKR_SECRET_NAME: Name/ARN of the secret containing Flickr credentials

    Secret JSON structure:
      {
        "api_key": "...",
        "api_secret": "...",
        "oauth_token": "...",
        "oauth_token_secret": "...",
        "user_nsid": "...",    # optional
        "username": "...",     # optional
        "access_level": "write"  # optional
      }
    """

    def __init__(self, *, store_token: bool = True) -> None:
        # store_token retained for signature compatibility; flickr_api handles auth differently.
        _ = store_token

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
        user_nsid = (str(data.get("user_nsid", "")).strip() or None)
        username = (str(data.get("username", "")).strip() or None)

        if not all([api_key, api_secret, oauth_token, oauth_token_secret]):
            raise RuntimeError("Flickr secret missing required keys (api_key, api_secret, oauth_token, oauth_token_secret)")

        # Configure the library keys
        flickr_api.set_keys(api_key=api_key, api_secret=api_secret)

        auth = flickr_api.auth.AuthHandler()

        TokenCls = getattr(flickr_api.auth, "OAuthToken", None)

        if TokenCls is not None:
            token_obj = TokenCls(oauth_token, oauth_token_secret)
        else:
            # Minimal shim with the required attributes
            class _Tok:
                def __init__(self, key, secret):
                    self.key = key
                    self.secret = secret
            token_obj = _Tok(oauth_token, oauth_token_secret)

        setattr(auth, "access_token", token_obj)

        # Optional: store user metadata on the handler if provided (not required for auth)
        if user_nsid:
            try:
                auth.user_nsid = user_nsid
            except Exception:
                pass
        if username:
            try:
                auth.username = username
            except Exception:
                pass

        flickr_api.set_auth_handler(auth)

    @staticmethod
    def _tags_to_string(tags: Optional[Iterable[str]]) -> Optional[str]:
        if not tags:
            return None
        return " ".join(t for t in tags if t)

    @staticmethod
    def _extract_photo_id(upload_resp) -> str:
        """
        flickr_api.upload typically returns a Photo object with attribute 'id'.
        Fall back to handling a dict or direct string/int if returned.
        """
        if upload_resp is None:
            raise RuntimeError("Empty response from Flickr upload")

        # Photo object path
        pid = getattr(upload_resp, "id", None)
        if pid:
            return str(pid)

        # Dict-like path
        if isinstance(upload_resp, dict):
            for key in ("id", "photoid", "photo_id"):
                if key in upload_resp and upload_resp[key]:
                    return str(upload_resp[key])

        # Primitive
        if isinstance(upload_resp, (str, int)):
            return str(upload_resp)

        raise RuntimeError(f"Unexpected upload response type: {type(upload_resp)!r}")

    @staticmethod
    def _to_flag(value: Optional[bool | int]) -> Optional[str]:
        """
        Coerce a boolean-like value to a "0"/"1" string accepted by Flickr API.
        Return None to omit the parameter entirely (lets Flickr use account defaults).
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return "1" if value else "0"
        try:
            return "1" if int(value) else "0"
        except Exception:
            return "0"

    def verify_auth(self) -> dict:
        """
        Simple auth sanity check (returns user info from flickr.test.login).
        Raises on error.
        """
        # flickr_api exposes methods directly in the module namespace
        return flickr_api.test.login()

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

        kwargs: dict = {
            "photo_file": file_path,
        }
        if title is not None:
            kwargs["title"] = title
        if description is not None:
            kwargs["description"] = description
        tag_str = self._tags_to_string(tags)
        if tag_str:
            kwargs["tags"] = tag_str

        # Preserve "0" by sending string flags and only omitting when None.
        flag = self._to_flag(is_public)
        if flag is not None:
            kwargs["is_public"] = flag
        flag = self._to_flag(is_friend)
        if flag is not None:
            kwargs["is_friend"] = flag
        flag = self._to_flag(is_family)
        if flag is not None:
            kwargs["is_family"] = flag

        if safety_level is not None:
            kwargs["safety_level"] = int(safety_level)
        if content_type is not None:
            kwargs["content_type"] = int(content_type)
        if hidden is not None:
            kwargs["hidden"] = int(hidden)

        resp = flickr_api.upload(**kwargs)
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
