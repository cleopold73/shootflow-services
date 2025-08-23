#!/usr/bin/env python3
# Python
import argparse
import json
import os
import sys
import base64
import html
from typing import List

import boto3
import flickr_api
from flickr_api.objects import Group  # type: ignore


def load_flickr_secret(*, region: str, secret_name: str) -> dict:
    sm = boto3.client("secretsmanager", region_name=region)
    resp = sm.get_secret_value(SecretId=secret_name)
    if "SecretString" in resp and resp["SecretString"]:
        raw = resp["SecretString"]
    else:
        raw = base64.b64decode(resp.get("SecretBinary", b"")).decode("utf-8")

    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            raise ValueError("SecretString JSON must be an object")
        return data
    except Exception:
        raise RuntimeError("Secret must be a JSON object with api_key, api_secret, oauth_token, oauth_token_secret")


def configure_flickr_api(creds: dict) -> str:
    """
    Configure flickr_api and return the resolved user NSID.
    Expects keys: api_key, api_secret, oauth_token, oauth_token_secret, optional user_nsid.
    """
    api_key = str(creds.get("api_key", "")).strip()
    api_secret = str(creds.get("api_secret", "")).strip()
    oauth_token = str(creds.get("oauth_token", "")).strip()
    oauth_token_secret = str(creds.get("oauth_token_secret", "")).strip()
    user_nsid = (str(creds.get("user_nsid", "")).strip() or None)

    if not all([api_key, api_secret, oauth_token, oauth_token_secret]):
        raise RuntimeError("Secret missing required keys (api_key, api_secret, oauth_token, oauth_token_secret)")

    flickr_api.set_keys(api_key=api_key, api_secret=api_secret)

    # Build auth handler with access token
    auth = flickr_api.auth.AuthHandler()
    TokenCls = getattr(flickr_api.auth, "OAuthToken", None)
    if TokenCls is not None:
        token_obj = TokenCls(oauth_token, oauth_token_secret)
    else:
        class _Tok:
            def __init__(self, key, secret):
                self.key = key
                self.secret = secret
        token_obj = _Tok(oauth_token, oauth_token_secret)
    setattr(auth, "access_token", token_obj)
    if user_nsid:
        try:
            auth.user_nsid = user_nsid
        except Exception:
            pass
    flickr_api.set_auth_handler(auth)

    # Resolve user id if not provided
    if not user_nsid:
        info = flickr_api.test.login()
        if isinstance(info, dict):
            user = info.get("user") or {}
            user_nsid = user.get("id") or user.get("nsid")
    if not user_nsid:
        raise RuntimeError("Could not resolve user NSID from secret or flickr.test.login()")

    return str(user_nsid)


def _normalize_groups(resp) -> List[dict]:
    """
    Normalize an iterable of Group-like objects into:
      [{"id": "...", "name": "..."}, ...]
    """
    def _to_text(v) -> str:
        if isinstance(v, bytes):
            try:
                s = v.decode("utf-8", "ignore")
            except Exception:
                s = str(v)
        elif v is None:
            s = ""
        else:
            s = str(v)
        return html.unescape(s).strip()

    out: List[dict] = []
    # Expect an iterable of Group objects (or dicts); no other shapes supported.
    try:
        iterator = iter(resp)
    except TypeError:
        return out

    for item in iterator:
        if isinstance(item, dict):
            gid = _to_text(item.get("id") or item.get("nsid"))
            name = _to_text(item.get("name") or item.get("path_alias") or gid)
        else:
            gid = _to_text(getattr(item, "id", None) or getattr(item, "nsid", None))
            name = _to_text(getattr(item, "name", None) or getattr(item, "path_alias", None) or gid)
        if gid:
            out.append({"id": gid, "name": name})
    return out


def list_groups_for_user(user_nsid: str) -> List[dict]:
    """
    List groups using only:
        from flickr_api.objects import Group
        Group(id=user_nsid).getGroups()
    Returns a list of dicts with id and name.
    """
    grp = Group(id=user_nsid)  # per discovered working path
    resp = grp.getGroups()
    return _normalize_groups(resp)


def get_group_description(group_id: str) -> str:
    """
    Retrieve a group's description using Group(id=group_id).getInfo().
    Returns an empty string if not available.
    """
    try:
        g = Group(id=group_id)
        info_fn = getattr(g, "getInfo", None)
        if callable(info_fn):
            info = info_fn()
            # Common shapes: top-level 'group' or flat dict
            container = info.get("group", info) if isinstance(info, dict) else {}
            desc = container.get("description")
            # Normalize to text
            if isinstance(desc, dict):
                val = desc.get("_content", "")
            else:
                val = desc
            if isinstance(val, bytes):
                try:
                    val = val.decode("utf-8", "ignore")
                except Exception:
                    val = str(val)
            return html.unescape(str(val or "")).strip()
    except Exception:
        pass
    return ""


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="List Flickr groups for the authenticated user.")
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="AWS region for Secrets Manager")
    p.add_argument("--secret-name", default=os.environ.get("FLICKR_SECRET_NAME"), help="Secrets Manager name for Flickr creds")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.region or not args.secret_name:
        print("Error: --region and --secret-name are required (or set AWS_REGION and FLICKR_SECRET_NAME env vars).", file=sys.stderr)
        return 2

    creds = load_flickr_secret(region=args.region, secret_name=args.secret_name)
    user_nsid = configure_flickr_api(creds)

    if args.verbose:
        print(f"Resolved user NSID: {user_nsid}")

    groups = list_groups_for_user(user_nsid)

    if not groups:
        print("No groups found or API returned empty list.")
        return 1

    print(f"Found {len(groups)} groups:")
    for g in groups:
        desc = get_group_description(g["id"])
        print(f"{g['id']}\t{g['name']}\t{desc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
