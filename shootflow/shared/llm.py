# Python
from __future__ import annotations

import base64
import json
import re
import time
import random
import logging
from typing import Any, Dict, Optional, Callable

import boto3
import requests
from requests.exceptions import ReadTimeout, Timeout, RequestException


def truncate(s: Optional[str], max_len: int) -> str:
    """
    Safe text truncation helper used for titles, captions, and group names.
    Adds an ellipsis character when truncated.
    """
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")


def strip_urls_and_html(s: Optional[str]) -> str:
    """
    Remove HTML tags and URLs from text, normalize whitespace, and reduce noisy separators.
    - Strips <tags> and their attributes (also removes script/style content).
    - Removes http/https/ftp URLs and common www. patterns and bare domains.
    - Unescapes HTML entities (e.g., &amp; → &).
    - Removes long runs of repeating punctuation/separators (e.g., ~~~~~, ******, -----, ________).
    - Collapses repeated whitespace to single spaces and trims.
    """
    if not s:
        return ""
    txt = str(s)

    # Unescape HTML entities first (e.g., &amp;)
    try:
        import html as _html
        txt = _html.unescape(txt)
    except Exception:
        pass

    # Remove script/style contents conservatively
    txt = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", " ", txt, flags=re.IGNORECASE | re.DOTALL)
    # Strip all remaining HTML tags
    txt = re.sub(r"<[^>]+>", " ", txt)

    # Remove URLs (http, https, ftp) and common www. patterns
    url_pattern = r"\b((?:https?|ftp)://\S+|www\.\S+)"
    txt = re.sub(url_pattern, " ", txt, flags=re.IGNORECASE)
    # Also remove bare domain.tld paths like example.com/foo when clearly a URL
    txt = re.sub(r"\b[\w.-]+\.(?:com|org|net|io|co|ai|photo|photos|me|info|dev|app)(?:/\S*)?", " ", txt, flags=re.IGNORECASE)

    # Collapse long runs of the same non-word, non-space character (decorative rulers)
    txt = re.sub(r"([^\w\s])\1{3,}", r"\1", txt)
    # Also collapse long runs of underscores (word char) used as separators → single underscore
    txt = re.sub(r"(_)\1{3,}", r"\1", txt)

    # Collapse whitespace
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def extract_json_object(text: str) -> Dict[str, Any]:
    """
    Try to parse a JSON object from a string.
    Prefer straight json.loads; on failure, attempt to locate the first {...} block.
    """
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("No JSON object found in response content")
    return json.loads(m.group(0))


def load_openai_api_key_from_secrets(secrets_client, secret_name: str) -> str:
    """
    Load an OpenAI API key from AWS Secrets Manager using a provided client.
    Accepts secrets where SecretString is either a raw key or a JSON object with {"api_key": "..."},
    or SecretBinary containing one of the above.
    """
    if not secret_name:
        raise RuntimeError("OPENAI_SECRET_NAME is not configured")

    resp = secrets_client.get_secret_value(SecretId=secret_name)

    if "SecretString" in resp and resp["SecretString"]:
        raw = resp["SecretString"]
    else:
        raw = base64.b64decode(resp.get("SecretBinary", b""))
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")

    api_key: Optional[str] = None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            api_key = obj.get("api_key")
        else:
            api_key = None
    except Exception:
        # Not JSON → treat as plain string
        api_key = str(raw)

    if not api_key:
        raise RuntimeError("OpenAI secret did not contain an api_key")

    return api_key


def load_openai_api_key(settings) -> str:
    """
    Shared loader that constructs a boto3 Secrets Manager client using settings
    and returns the OpenAI API key as a string.
    """
    secret_name = getattr(settings, "OPENAI_SECRET_NAME", None)
    if not secret_name:
        raise RuntimeError("OPENAI_SECRET_NAME is not configured")
    region = getattr(settings, "AWS_REGION", None)
    sm = boto3.client("secretsmanager", region_name=region)
    return load_openai_api_key_from_secrets(sm, secret_name)


def post_with_retries(
    url: str,
    headers: Dict[str, str],
    body: Dict[str, Any],
    *,
    connect_timeout_s: float,
    read_timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    post_fn: Callable[..., requests.Response] | None = None,
    log: logging.Logger | None = None,
) -> requests.Response:
    """
    HTTP POST with simple exponential backoff + jitter.
    Retries on timeouts, connection errors, and 5xx responses.

    Parameters:
      - post_fn: injectable function (defaults to requests.post) to ease testing/patching
      - log: optional logger (defaults to a module logger)
    """
    _log = log or logging.getLogger(__name__)
    _post = post_fn or requests.post

    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt <= max_retries:
        try:
            resp = _post(
                url,
                headers=headers,
                json=body,
                timeout=(connect_timeout_s, read_timeout_s),
            )
            if resp.status_code >= 500:
                _log.warning(
                    "LLM server error status=%s attempt=%s body_trunc=%s",
                    resp.status_code,
                    attempt,
                    str(body.get("model"))[:40],
                )
            else:
                return resp
        except (ReadTimeout, Timeout) as e:
            last_exc = e
            _log.warning("LLM request timeout attempt=%s url=%s error=%s", attempt, url, e)
        except RequestException as e:
            last_exc = e
            _log.warning("LLM request network error attempt=%s url=%s error=%s", attempt, url, e)

        attempt += 1
        if attempt <= max_retries:
            sleep_s = (backoff_base_s ** attempt) + random.uniform(0, 0.25 * backoff_base_s)
            time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    raise RuntimeError("LLM request failed after retries without specific exception")
