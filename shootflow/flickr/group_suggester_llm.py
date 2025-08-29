from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional


from .flickr import FlickrAPIClient
from shootflow.shared.llm import (
    truncate,
    strip_urls_and_html,
    extract_json_object,
    load_openai_api_key,
    post_with_retries,
)

logger = logging.getLogger(__name__)


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
    api_key = load_openai_api_key(settings)

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
        name = truncate(g.get("name"), name_max)
        raw_desc = g.get("description")
        clean_desc = strip_urls_and_html(raw_desc)
        desc = truncate(clean_desc, desc_max)
        candidates.append({"id": gid, "name": name, "description": desc})

    title = truncate(photo_item.get("title"), title_max)
    caption = truncate(photo_item.get("caption"), caption_max)
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
        "Specifically use the camera brand/make (EXIF:Make or EXIF:Model) to match any brand-focused groups when applicable (e.g., Pentax, Canon, Sony, Fujifilm, Leica, Olympus, Panasonic). "
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

    # Optional: tag requests in OpenAI dashboard and emit prompt for debugging
    try:
        user_tag = getattr(settings, "OPENAI_USER_TAG", None)
        if user_tag:
            body["user"] = str(user_tag)
    except Exception:
        pass

    if getattr(settings, "LLM_LOG_PROMPT", False):
        try:
            logger.info(
                "llm_prompt messages=%s model=%s",
                body.get("messages"),
                body.get("model"),
            )
            # Emit a redacted curl for easy reproduction in Playground or curl
            redacted_key = (str(api_key[:7]) + "…redacted") if isinstance(api_key, str) and len(api_key) > 7 else "***"
            curl = (
                f"curl -sS -X POST '{base_url}/chat/completions' "
                f"-H 'Authorization: Bearer {redacted_key}' -H 'Content-Type: application/json' "
                f"-d '{json.dumps({k: v for k, v in body.items() if k != 'response_format'}, ensure_ascii=False)}'"
            )
            logger.info("llm_prompt_replay curl=%s", curl)
            logger.info("Tip: Paste system+user messages into OpenAI Playground to ask follow-up questions.")
        except Exception:
            # Never let logging break main flow
            pass

    url = f"{base_url}/chat/completions"
    try:
        resp = post_with_retries(
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

    obj = extract_json_object(content)
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
