#!/usr/bin/env python3
# python
import base64
import json
import os
import tempfile
from typing import Iterable, Optional
import logging
import time

import boto3
import flickr_api

# requests is an indirect dep of flickr-api; used only for exception types
try:
    import requests
except Exception:  # Fallback if requests import path changes
    requests = None  # type: ignore

from shootflow.shared.config import settings

logger = logging.getLogger(__name__)


class FlickrAPIClient:
    """
    Thin wrapper around flickr_api suitable for expansion.
    Credentials are loaded automatically from AWS Secrets Manager.

    settings.toml keys:
      - AWS_REGION: AWS region for Secrets Manager
      - FLICKR_SECRET_NAME: Name/ARN of the secret containing Flickr credentials

    Optional tunables (with defaults):
      - FLICKR_UPLOAD_TIMEOUT_SECONDS = 120
      - FLICKR_UPLOAD_MAX_RETRIES = 3
      - FLICKR_UPLOAD_BACKOFF_SECONDS = 2.0

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

        self._upload_timeout_s: int = settings.FLICKR_UPLOAD_TIMEOUT_SECONDS
        self._upload_max_retries: int = settings.FLICKR_UPLOAD_MAX_RETRIES
        self._upload_backoff_seconds: float = settings.FLICKR_UPLOAD_BACKOFF_SECONDS

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
        flickr_api.set_timeout(self._upload_timeout_s)

        # Keep handles for subsequent calls
        self._auth_handler = auth
        # Keep the authenticated user NSID (if provided) for subsequent calls
        self._user_nsid = user_nsid

    @staticmethod
    def _tags_to_string(tags: Optional[Iterable[str]]) -> Optional[str]:
        """
        Convert an iterable of tag strings into Flickr's expected space-delimited format.
        - Commas are NOT separators in Flickr; we split incoming comma-separated entries to be safe.
        - Multi-word tags must be quoted.
        - Double quotes inside a tag are escaped as \"
        - Empty items are dropped, duplicates are removed (order-preserving).
        """
        if not tags:
            return None

        normalized: list[str] = []

        def _emit(tag: str) -> None:
            t = str(tag).strip()
            if not t:
                return
            # Escape embedded quotes
            t = t.replace('"', '\\"')
            # Quote multi-word tags
            if any(ch.isspace() for ch in t):
                t = f'"{t}"'
            normalized.append(t)

        for raw in tags:
            if raw is None:
                continue
            s = str(raw).strip()
            if not s:
                continue
            # Normalize accidental comma-separated inputs into separate tags
            if "," in s:
                for part in (p.strip() for p in s.split(",")):
                    if part:
                        _emit(part)
            else:
                _emit(s)

        if not normalized:
            return None

        # Deduplicate while preserving order
        deduped = list(dict.fromkeys(normalized))

        # Optional: cap to a reasonable upper bound
        MAX_TAGS = 75
        deduped = deduped[:MAX_TAGS]

        return " ".join(deduped)

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

    def _resolve_user_nsid(self) -> Optional[str]:
        """
        Resolve the authenticated user's NSID using multiple sources:
          1) Stored during init from the secret (preferred)
          2) From the auth handler if available
          3) From flickr_api.test.login() response
        """
        if getattr(self, "_user_nsid", None):
            return self._user_nsid
        # Try auth handler
        try:
            auth = getattr(self, "_auth_handler", None)
            if auth and getattr(auth, "user_nsid", None):
                self._user_nsid = auth.user_nsid  # cache
                return self._user_nsid
        except Exception:
            pass
        # Try flickr.test.login
        try:
            info = flickr_api.test.login()
            # Common shapes: {"user": {"id": "...", "nsid": "..."}} or nested username
            if isinstance(info, dict):
                user = info.get("user") or {}
                uid = user.get("id") or user.get("nsid")
                if uid:
                    self._user_nsid = str(uid)
                    return self._user_nsid
        except Exception:
            pass
        return None

    def _upload_with_retries(self, **kwargs):
        """
        Call flickr_api.upload with retries on transient network timeouts.
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, self._upload_max_retries + 1):
            try:
                return flickr_api.upload(**kwargs)
            except Exception as exc:
                is_timeout = False
                is_conn_err = False

                if requests is not None:
                    is_timeout = isinstance(exc, requests.exceptions.ReadTimeout)  # type: ignore[attr-defined]
                    is_conn_err = isinstance(exc, requests.exceptions.ConnectionError)  # type: ignore[attr-defined]
                else:
                    # Fallback heuristic by class name if requests import failed
                    name = type(exc).__name__
                    is_timeout = name == "ReadTimeout"
                    is_conn_err = name == "ConnectionError"

                if is_timeout or is_conn_err:
                    last_exc = exc
                    if attempt < self._upload_max_retries:
                        sleep_s = self._upload_backoff_seconds * (2 ** (attempt - 1))
                        logger.warning(
                            f"Flickr upload attempt {attempt} failed ({type(exc).__name__}). "
                            f"Retrying in {sleep_s:.1f}s..."
                        )
                        time.sleep(sleep_s)
                        continue
                    # Final attempt failed: break and raise a clear error below
                    break
                # Non-retryable error: re-raise immediately
                raise
        # If we exit loop without returning, retries were exhausted
        raise RuntimeError(
            f"Flickr upload failed after {self._upload_max_retries} attempts: {last_exc}"
        ) from last_exc

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

        # Use retrying wrapper
        resp = self._upload_with_retries(**kwargs)
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

    def list_member_groups(
        self,
        *,
        include_descriptions: bool = True,
        max_groups: int = 200,
    ) -> list[dict]:
        """
        Return a list of groups the authenticated user is a member of.
        Each item: {"id": str, "name": str, "description": Optional[str]}.

        Simplified implementation using flickr_api.objects.Group(id=user_nsid).getGroups().
        """
        def _to_text(v) -> str:
            if isinstance(v, bytes):
                try:
                    return v.decode("utf-8", "ignore")
                except Exception:
                    return str(v)
            return "" if v is None else str(v)

        user_id = self._resolve_user_nsid()
        if not user_id:
            logger.warning("Could not resolve user NSID; returning empty list")
            return []

        # Load Group object and fetch groups
        try:
            from flickr_api.objects import Group  # type: ignore
        except Exception as e:
            logger.warning("Unable to import flickr_api.objects.Group: %s", e, exc_info=False)
            return []

        try:
            resp = Group(id=user_id).getGroups()
        except Exception as e:
            logger.warning("Group(id=user).getGroups() failed: %s", e, exc_info=False)
            return []

        results: list[dict] = []

        # Iterate custom iterable of Group-like objects
        try:
            iterator = iter(resp)
        except TypeError:
            return results

        for item in iterator:
            if isinstance(item, dict):
                gid = _to_text(item.get("id") or item.get("nsid"))
                name = _to_text(item.get("name") or item.get("path_alias") or gid)
            else:
                gid = _to_text(getattr(item, "id", None) or getattr(item, "nsid", None))
                name = _to_text(getattr(item, "name", None) or getattr(item, "path_alias", None) or gid)
            if gid:
                results.append({"id": gid, "name": name, "description": None})
            if len(results) >= max_groups:
                break

        # Optionally enrich with descriptions via helper
        if include_descriptions and results:
            for entry in results:
                try:
                    entry["description"] = self.get_group_description(entry["id"]) or None
                except Exception:
                    # Keep name/id even if description fails
                    pass

        return results

    def get_group_description(self, group_id: str) -> Optional[str]:
        """
        Retrieve a group's description using Group(id=group_id).getInfo().
        Returns None if unavailable.
        """
        try:
            from flickr_api.objects import Group  # type: ignore
            info = Group(id=group_id).getInfo()
            container = info.get("group", info) if isinstance(info, dict) else {}
            desc = container.get("description")
            if isinstance(desc, dict):
                val = desc.get("_content", "")
            else:
                val = desc
            if isinstance(val, bytes):
                try:
                    val = val.decode("utf-8", "ignore")
                except Exception:
                    val = str(val)
            text = str(val or "").strip()
            return text or None
        except Exception:
            return None

    def add_photo_to_group(self, photo_id: str, group_id: str) -> None:
        """
        Add an existing photo to a Flickr group pool (flickr.groups.pools.add).
        Best-effort: logs failures (including photo_id and group_id) instead of raising,
        and treats 'Pending Queue' moderation responses as success.
        Raises:
            ValueError: if inputs are missing
        """
        if not photo_id or not group_id:
            raise ValueError("photo_id and group_id are required")
        try:
            from flickr_api.objects import Group  # type: ignore
        except Exception as e:
            logger.warning("Unable to import flickr_api.api.flickr: %s", e, exc_info=False)
            raise
        try:
            Group(id=group_id).addPhoto(photo_id=photo_id)
        except Exception as e:
            # Treat "Pending Queue" as success (group moderation queue)
            msg = str(e)
            if "pending queue" in msg.lower():
                logger.info(
                    "add_photo_to_group: treated as success (pending queue). photo_id=%s group_id=%s detail=%s",
                    photo_id,
                    group_id,
                    msg,
                )
                return
            logger.warning(
                "add_photo_to_group_failed photo_id=%s group_id=%s error=%s",
                photo_id,
                group_id,
                msg,
            )
            return
