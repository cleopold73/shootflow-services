#!/usr/bin/env python3
import json
import os
import sys
import time

import flickrapi


def _clean(v: str) -> str:
    v = (v or "").strip()
    if len(v) >= 2 and ((v[0] == v[-1] == "'") or (v[0] == v[-1] == '"')):
        v = v[1:-1].strip()
    return v


def _require_env(name: str) -> str:
    val = _clean(os.getenv(name, ""))
    if not val:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return val


def _extract_tokens(ret, flickr):
    """
    Normalize tokens from various flickrapi return shapes:
    - tuple/list: (token, secret)
    - dict-like: {'oauth_token': ..., 'oauth_token_secret': ..., 'user_nsid': ..., 'username': ...}
    - object: attributes oauth_token, oauth_token_secret, user_nsid, username
    Also checks flickr.token_cache as a fallback.
    """
    tok = None
    sec = None
    nsid = None
    username = None

    def from_obj(obj):
        t = getattr(obj, "oauth_token", None)
        s = getattr(obj, "oauth_token_secret", None)
        uid = getattr(obj, "user_nsid", None)
        uname = getattr(obj, "username", None)
        return t, s, uid, uname

    # Direct return: tuple/list
    if isinstance(ret, (tuple, list)) and len(ret) >= 2:
        tok, sec = str(ret[0]), str(ret[1])

    # Direct return: dict-like
    elif isinstance(ret, dict):
        tok = ret.get("oauth_token") or ret.get("token")
        sec = ret.get("oauth_token_secret") or ret.get("secret")
        nsid = ret.get("user_nsid")
        username = ret.get("username")

    # Direct return: object with attributes
    elif hasattr(ret, "__class__"):
        tok, sec, nsid, username = from_obj(ret)

    # Fallback to token_cache if still missing
    if (not tok or not sec) and hasattr(flickr, "token_cache") and getattr(flickr.token_cache, "token", None):
        cache_obj = flickr.token_cache.token
        if isinstance(cache_obj, dict):
            tok = tok or cache_obj.get("oauth_token") or cache_obj.get("token")
            sec = sec or cache_obj.get("oauth_token_secret") or cache_obj.get("secret")
            nsid = nsid or cache_obj.get("user_nsid")
            username = username or cache_obj.get("username")
        else:
            t2, s2, uid2, un2 = from_obj(cache_obj)
            tok = tok or t2
            sec = sec or s2
            nsid = nsid or uid2
            username = username or un2

    return tok, sec, nsid, username


def main():
    api_key = _require_env("FLICKR_API_KEY")
    api_secret = _require_env("FLICKR_API_SECRET")
    perms = _clean(os.getenv("FLICKR_PERMS") or "write")

    print(f"[info] Epoch now: {int(time.time())}")

    flickr = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json", store_token=False, cache=True)

    print("[oauth] Requesting request token (callback=oob)...")
    try:
        flickr.get_request_token(oauth_callback="oob")
    except Exception as e:
        print(f"[oauth] Failed to get request token: {e}", file=sys.stderr)
        sys.exit(3)

    try:
        authorize_url = flickr.auth_url(perms=perms)
    except Exception as e:
        print(f"[oauth] Failed to build authorize URL: {e}", file=sys.stderr)
        sys.exit(4)

    print("\nOpen this URL in your browser, authorize the app, and copy the verifier (PIN):")
    print(authorize_url)

    verifier = input("\nVerifier (PIN): ").strip()
    if not verifier:
        print("No verifier provided.", file=sys.stderr)
        sys.exit(5)

    print("[oauth] Exchanging verifier for access token...")
    try:
        ret = flickr.get_access_token(verifier)
    except Exception as e:
        print(f"[oauth] Failed to exchange access token: {e}", file=sys.stderr)
        sys.exit(6)

    oauth_token, oauth_token_secret, user_nsid, username = _extract_tokens(flickr.token_cache.token, flickr)

    oauth_token = flickr.token_cache.token.token
    oauth_token_secret  = flickr.token_cache.token.token_secret

    if not oauth_token or not oauth_token_secret:
        print(f"[oauth] Could not extract tokens from response/cache: {ret}", file=sys.stderr)
        sys.exit(7)

    result = {
        "api_key": "<YOUR_API_KEY>",
        "api_secret": "<YOUR_API_SECRET>",
        "oauth_token": str(oauth_token),
        "oauth_token_secret": str(oauth_token_secret),
        "user_nsid": user_nsid if user_nsid is not None else "",
        "username": username if username is not None else "",
        "perms": perms,
    }

    print("\nSuccess. Store these credentials securely:")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)