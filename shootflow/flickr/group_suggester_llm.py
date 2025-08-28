from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
import base64
import time
import random

import requests
import boto3
from requests.exceptions import ReadTimeout, Timeout, RequestException

from .flickr import FlickrAPIClient

logger = logging.getLogger(__name__)


def _load_openai_api_key(settings) -> str:
    """
    Load OpenAI API key from AWS Secrets Manager.
    Expects settings.OPENAI_SECRET_NAME to point to a secret containing either:
      - {"api_key": "..."} in SecretString JSON, or
      - raw SecretString containing the key directly, or
      - SecretBinary with one of the above.
    """
    secret_name = settings.OPENAI_SECRET_NAME
    if not secret_name:
        raise RuntimeError("OPENAI_SECRET_NAME is not configured")

    region = settings.AWS_REGION
    sm = boto3.client("secretsmanager", region_name=region)
    resp = sm.get_secret_value(SecretId=secret_name)

    raw: str
    if "SecretString" in resp and resp["SecretString"]:
        raw = resp["SecretString"]
    else:
        raw = base64.b64decode(resp.get("SecretBinary", b"")).decode("utf-8")

    api_key: Optional[str] = None
    try:
        # Try JSON form
        obj = json.loads(raw)
        if isinstance(obj, dict):
            api_key = obj.get("api_key")
        else:
            # Not a dict → treat as plain string below
            api_key = None
    except Exception:
        # Not JSON → treat as plain string
        api_key = raw

    if not api_key:
        raise RuntimeError("OpenAI secret did not contain an api_key")

    return api_key


def _truncate(s: Optional[str], max_len: int) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")


def _extract_json_object(text: str) -> Dict[str, Any]:
    """
    Try to parse a JSON object from the response content.
    Prefer straight json.loads; on failure, attempt to locate the first {...} block.
    """
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(m.group(0))


def _post_with_retries(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    connect_timeout_s: float,
    read_timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
) -> requests.Response:
    """
    POST with simple exponential backoff + jitter.
    Retries on timeouts and 5xx responses.
    """
    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt <= max_retries:
        try:
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                timeout=(connect_timeout_s, read_timeout_s),
            )
            if resp.status_code >= 500:
                # Server error → retry
                logger.warning(
                    "LLM server error status=%s attempt=%s body_trunc=%s",
                    resp.status_code,
                    attempt,
                    str(body.get("model"))[:40],
                )
            else:
                return resp
        except (ReadTimeout, Timeout) as e:
            last_exc = e
            logger.warning("LLM request timeout attempt=%s url=%s error=%s", attempt, url, e)
        except RequestException as e:
            # Network errors that may be transient → retry
            last_exc = e
            logger.warning("LLM request network error attempt=%s url=%s error=%s", attempt, url, e)

        # Backoff if we will retry again
        attempt += 1
        if attempt <= max_retries:
            # exponential backoff with jitter
            sleep_s = (backoff_base_s ** attempt) + random.uniform(0, 0.25 * backoff_base_s)
            time.sleep(sleep_s)

    # If we exhausted retries, raise last exception or return last response if present
    if last_exc:
        raise last_exc
    raise RuntimeError("LLM request failed after retries without specific exception")


def suggest_groups_via_llm(
    photo_item: Dict[str, Any],
    *,
    flickr_client: Optional[FlickrAPIClient] = None,
    settings,
) -> List[str]:
    """
    Use an OpenAI-compatible chat API to suggest best groups for a photo.
    - Fetches user's groups + descriptions from Flickr (best effort).
    - Sends photo title/caption/tags and candidate groups to the LLM.
    - Expects JSON: {"selected_group_ids": ["<nsid>", ...]}.
    Returns a list of group IDs present in the candidate set.
    """
    api_key = _load_openai_api_key(settings)

    base_url = settings.OPENAI_BASE_URL.rstrip("/")
    model = settings.OPENAI_CHAT_MODEL
    max_return = settings.LLM_MAX_GROUPS
    max_candidates = settings.LLM_MAX_CANDIDATE_GROUPS
    connect_timeout_s = settings.LLM_HTTP_CONNECT_TIMEOUT_SECONDS
    read_timeout_s = settings.LLM_HTTP_READ_TIMEOUT_SECONDS
    max_retries = settings.LLM_HTTP_RETRIES
    backoff_base_s = settings.LLM_HTTP_BACKOFF_BASE_SECONDS
    name_max = settings.LLM_GROUP_NAME_MAX_LEN
    desc_max = settings.LLM_GROUP_DESC_MAX_LEN
    title_max = settings.LLM_TITLE_MAX_LEN
    caption_max = settings.LLM_CAPTION_MAX_LEN

    client = flickr_client or FlickrAPIClient()
    groups = client.list_member_groups(include_descriptions=True, max_groups=max_candidates)

    # Build compact candidate list
    candidates: List[Dict[str, str]] = []
    for g in groups:
        gid = str(g.get("id") or "").strip()
        if not gid:
            continue
        name = _truncate(g.get("name"), name_max)
        desc = _truncate(g.get("description"), desc_max)
        candidates.append({"id": gid, "name": name, "description": desc})

    title = _truncate(photo_item.get("title"), title_max)
    caption = _truncate(photo_item.get("caption"), caption_max)
    tags = [str(t).strip() for t in (photo_item.get("tags") or []) if str(t).strip()]

    # Parse EXIF (stored as JSON string in DynamoDB) if present
    raw_exif = photo_item.get("exif")
    exif_obj: Dict[str, Any] = {}
    if isinstance(raw_exif, str):
        try:
            exif_obj = json.loads(raw_exif)
        except Exception:
            exif_obj = {}
    elif isinstance(raw_exif, dict):
        exif_obj = raw_exif

    system_msg = (
        "You are assisting with selecting appropriate Flickr groups for a photo. "
        "Only choose groups that clearly match the subject, style, or technical characteristics as described in the group's description and rules. "
        "You will receive the photo's title, caption, tags, and EXIF metadata. Use camera and exposure details (e.g., camera make/model, lens, focal length, aperture/f-number, shutter speed/exposure time, ISO, white balance, flash, 35mm equivalent, long exposure, macro, night, etc.) to match groups that explicitly focus on those attributes when relevant. "
        "When gear-specific groups (brand/model/lens/focal length) apply, you may select them; otherwise, prioritize subject/style and technique groups. Do not select off-topic, spammy, or rule-violating groups. Prefer quality over quantity. "
        "Respond strictly as a JSON object with the schema: "
        '{ "selected_group_ids": ["<nsid>", "..."] }.'
    )

    user_payload = {
        "photo": {"title": title, "caption": caption, "tags": tags, "exif": exif_obj},
        "candidate_groups": candidates,
        "max_groups": max_return,
        "instructions": "Choose up to max_groups group IDs. Only IDs present in candidate_groups.",
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        # Intentionally omit 'temperature' to support providers/models that disallow overrides
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }

    url = f"{base_url}/chat/completions"
    try:
        resp = _post_with_retries(
            url,
            headers,
            body,
            connect_timeout_s=connect_timeout_s,
            read_timeout_s=read_timeout_s,
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
        )
    except Exception as e:
        # Graceful degradation: no groups rather than raising, since the caller treats groups as optional
        logger.warning(
            "llm_group_suggestion_failed url=%s error=%s title=%s tags_count=%s",
            url,
            e,
            title,
            len(tags),
            exc_info=True,
        )
        return []

    if resp.status_code >= 400:
        # Non-retryable client error; log and return empty
        logger.warning("LLM request failed status=%s body=%s", resp.status_code, resp.text)
        return []

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("Unexpected LLM response shape: %s; data=%r", e, data)
        return []

    obj = _extract_json_object(content)
    ids = obj.get("selected_group_ids") or []
    # Filter to known candidates and enforce max
    candidate_ids = {c["id"] for c in candidates}
    selected = [gid for gid in ids if gid in candidate_ids]
    if len(selected) > max_return:
        selected = selected[:max_return]

    # Log both IDs and human-friendly names of selected groups
    id_to_name = {c["id"]: c.get("name", "") for c in candidates}
    selected_names = [id_to_name.get(gid, "") for gid in selected]
    logger.info("llm_group_suggestion selected_ids=%s selected_names=%s", selected, selected_names)
    return selected
