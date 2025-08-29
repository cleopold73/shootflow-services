# Python
import logging
import json
import requests
from typing import Any, Dict, List

from shootflow.shared.ddb_repo import PhotoRepository
from shootflow.flickr import FlickrAPIClient
from shootflow.shared.logger import setup_basic_logging
from shootflow.shared.config import settings
from shootflow.shared.llm import (
    load_openai_api_key,
    post_with_retries,
    extract_json_object,
    truncate,
)

logger = logging.getLogger(__name__)


def _generate_caption_and_hashtags(photo_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Use the LLM to generate an Instagram caption and hashtags from title/description/tags.
    Returns {"caption": str, "hashtags": ["#..."]} or an empty default on failure.
    """
    try:
        api_key = load_openai_api_key(settings)
    except Exception as e:
        logger.warning("openai_api_key_load_failed error=%s", e)
        return {"caption": "", "hashtags": []}

    base_url = settings.OPENAI_BASE_URL.rstrip("/")
    model = settings.OPENAI_CHAT_MODEL
    # Reuse truncation limits from LLM settings
    title_max = getattr(settings, "LLM_TITLE_MAX_LEN", 200)
    caption_max = getattr(settings, "LLM_CAPTION_MAX_LEN", 800)

    title = truncate(photo_item.get("title"), title_max)
    desc = truncate(photo_item.get("caption"), caption_max)
    tags = [str(t).strip() for t in (photo_item.get("tags") or []) if str(t).strip()]

    system_msg = (
        "You are an assistant that writes Instagram post text for photographs. "
        "Given the photo's title, description, and keyword tags, craft: "
        "(1) a concise, engaging caption suitable for Instagram (avoid quotes around the whole caption; keep it human and authentic), and "
        "(2) a list of relevant hashtags (start each with #, avoid duplicates, no more than 25, prefer specific over generic). "
        "Do NOT include any external links. Respond strictly as a JSON object: "
        '{"caption": "...", "hashtags": ["#tag1", "#tag2", ...]}.'
    )

    user_payload = {
        "title": title,
        "description": desc,
        "tags": tags,
        "constraints": {
            "max_hashtags": 25,
            "tone": "authentic, descriptive, positive",
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }

    # Optional: dashboard user tag and prompt logging based on shared settings
    try:
        user_tag = getattr(settings, "OPENAI_USER_TAG", None)
        if user_tag:
            body["user"] = str(user_tag)
    except Exception:
        pass

    if getattr(settings, "LLM_LOG_PROMPT", False):
        try:
            redacted = "***" if not isinstance(api_key, str) else (api_key[:7] + "…redacted")
            logger.info("ig_llm_prompt messages=%s model=%s", body.get("messages"), model)
            logger.info(
                "ig_llm_curl curl -sS -X POST '%s/chat/completions' -H 'Authorization: Bearer %s' -H 'Content-Type: application/json' -d '%s'",
                base_url,
                redacted,
                json.dumps({k: v for k, v in body.items() if k != 'response_format'}, ensure_ascii=False),
            )
        except Exception:
            pass

    url = f"{base_url}/chat/completions"
    try:
        resp = post_with_retries(
            url,
            headers,
            body,
            connect_timeout_s=settings.LLM_HTTP_CONNECT_TIMEOUT_SECONDS,
            read_timeout_s=settings.LLM_HTTP_READ_TIMEOUT_SECONDS,
            max_retries=settings.LLM_HTTP_RETRIES,
            backoff_base_s=settings.LLM_HTTP_BACKOFF_BASE_SECONDS,
        )
    except Exception as e:
        logger.warning("ig_caption_llm_failed error=%s", e, exc_info=True)
        return {"caption": "", "hashtags": []}

    if resp.status_code >= 400:
        logger.warning("ig_caption_llm_http_error status=%s body=%s", resp.status_code, resp.text)
        return {"caption": "", "hashtags": []}

    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
        obj = extract_json_object(content)
        cap = str(obj.get("caption") or "").strip()
        hashtags: List[str] = []
        for h in obj.get("hashtags") or []:
            s = str(h).strip()
            if not s:
                continue
            if not s.startswith("#"):
                s = "#" + s
            hashtags.append(s)
        # De-duplicate hashtags while preserving order and limit to 25
        seen = set()
        uniq = []
        for h in hashtags:
            if h.lower() not in seen:
                seen.add(h.lower())
                uniq.append(h)
        if len(uniq) > 25:
            uniq = uniq[:25]
        return {"caption": cap, "hashtags": uniq}
    except Exception as e:
        logger.warning("ig_caption_llm_parse_error error=%s data=%r", e, data)
        return {"caption": "", "hashtags": []}


def main():
    setup_basic_logging()
    flickr_client = FlickrAPIClient()
    db = PhotoRepository()

    selected = db.get_photo_with_highest_flickr_score()
    if not selected:
        logger.info("instagram_pick_no_eligible_items")
        return
    photo_id, flickr_id = selected

    # Fetch details for LLM
    item = db.get_item(photo_id) or {}

    # Generate caption and hashtags via LLM and print to screen
    ch = _generate_caption_and_hashtags(item)
    caption = ch.get("caption", "")
    hashtags = ch.get("hashtags", [])
    # Output to stdout so CLI users see the content clearly
    print("INSTAGRAM CAPTION:\n" + caption)
    if hashtags:
        print("\nHASHTAGS:\n" + " ".join(hashtags))

    # Download the image file
    url = flickr_client.get_download_url(flickr_id)
    with requests.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        with open(f"{flickr_id}.jpg", "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:  # skip keep-alive chunks
                    f.write(chunk)

    # Mark as prepared for Instagram
    db.update_instagram_posted(photo_id, "POSTED")


if __name__ == "__main__":
    main()