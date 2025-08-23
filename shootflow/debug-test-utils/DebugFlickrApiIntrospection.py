#!/usr/bin/env python3
# Python
import argparse
import base64
import json
import os
import sys
from typing import Any, Dict, List, Optional

import boto3
import flickr_api

# Try to import the low-level REST caller if available
try:
    from flickr_api.api import flickr_call  # type: ignore
except Exception:
    flickr_call = None  # type: ignore


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


def print_capabilities() -> None:
    print("flickr_api version:", getattr(flickr_api, "__version__", "unknown"))
    print("Has flickr_call:", bool(flickr_call))
    # Check optional submodules
    try:
        import flickr_api.people as people  # type: ignore
        print("Has module flickr_api.people:", True, "has getGroups:", hasattr(people, "getGroups"))
    except Exception:
        print("Has module flickr_api.people:", False)
    try:
        import flickr_api.objects as objects  # type: ignore
        Person = getattr(objects, "Person", None)
        has_method = hasattr(Person, "getGroups") if Person else False
        print("Has module flickr_api.objects:", True, "has Person:", bool(Person), "Person.getGroups:", has_method)
    except Exception:
        print("Has module flickr_api.objects:", False)
    try:
        import flickr_api.groups_pools as groups_pools  # type: ignore
        print("Has module flickr_api.groups_pools:", True, "has getGroups:", hasattr(groups_pools, "getGroups"))
    except Exception:
        print("Has module flickr_api.groups_pools:", False)
    try:
        GroupCls = getattr(flickr_api, "Group", None)
        has_method = hasattr(GroupCls, "getGroups") if GroupCls else False
        methods = []
        if GroupCls is not None:
            try:
                methods = sorted(
                    [m for m in dir(GroupCls) if callable(getattr(GroupCls, m, None)) and not m.startswith("_")]
                )
            except Exception:
                methods = []
        print(
            "Has flickr_api.Group:",
            bool(GroupCls),
            "Group.getGroups:",
            has_method,
            "Group methods:",
            ", ".join(methods[:20]),
        )
    except Exception:
        print("Has flickr_api.Group:", False)


def try_people_get_groups(user_nsid: str) -> Optional[dict]:
    if not flickr_call:
        return None
    try:
        return flickr_call(method="flickr.people.getGroups", user_id=user_nsid)  # type: ignore
    except Exception as e:
        print("people.getGroups failed:", repr(e))
        return None


def try_pools_get_groups() -> Optional[dict]:
    if not flickr_call:
        return None
    try:
        return flickr_call(method="flickr.groups.pools.getGroups")  # type: ignore
    except Exception as e:
        print("groups.pools.getGroups failed:", repr(e))
        return None


def try_groups_pools_module_get_groups() -> Optional[dict]:
    # High-level module call: flickr_api.groups_pools.getGroups
    try:
        import flickr_api.groups_pools as gp  # type: ignore
        if not hasattr(gp, "getGroups"):
            print("groups_pools.getGroups not available on this version")
            return None
        resp = gp.getGroups()
        return resp if isinstance(resp, dict) else {"_raw": str(resp)}
    except Exception as e:
        print("groups_pools.getGroups (module) failed:", repr(e))
        return None


def try_person_object_groups(user_nsid: str) -> Optional[dict]:
    # Some versions may expose a Person object with getGroups; many do not.
    try:
        from flickr_api.objects import Group  # type: ignore
        group = Group(id=user_nsid)
        resp = group.getGroups()
        # Wrap any non-dict response into a dict for printing
        return resp if isinstance(resp, dict) else {"_raw": str(resp)}
    except Exception as e:
        print("Person.getGroups path failed:", repr(e))
        return None


def normalize_groups(resp: Any) -> List[dict]:
    out: List[dict] = []
    if isinstance(resp, dict):
        groups_field = resp.get("groups") or {}
        group_list = groups_field.get("group") if isinstance(groups_field, dict) else None
        if isinstance(group_list, list):
            for g in group_list:
                if isinstance(g, dict):
                    gid = str(g.get("nsid") or g.get("id") or "").strip()
                    name = str(g.get("name") or g.get("path_alias") or gid).strip()
                    if gid:
                        out.append({"id": gid, "name": name})
        return out
    if isinstance(resp, list):
        for item in resp:
            if isinstance(item, dict):
                gid = str(item.get("nsid") or item.get("id") or "").strip()
                name = str(item.get("name") or item.get("path_alias") or gid).strip()
            else:
                gid = str(getattr(item, "nsid", getattr(item, "id", "")) or "").strip()
                name = str(getattr(item, "name", getattr(item, "path_alias", gid)) or "").strip()
            if gid:
                out.append({"id": gid, "name": name})
        return out
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Debug installed flickr_api capabilities and list groups.")
    p.add_argument("--region", default=os.environ.get("AWS_REGION"), help="AWS region for Secrets Manager")
    p.add_argument("--secret-name", default=os.environ.get("FLICKR_SECRET_NAME"), help="Secrets Manager name for Flickr creds")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output (prints raw JSON)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.region or not args.secret_name:
        print("Error: --region and --secret-name are required (or set AWS_REGION and FLICKR_SECRET_NAME).", file=sys.stderr)
        return 2

    creds = load_flickr_secret(region=args.region, secret_name=args.secret_name)
    user_nsid = configure_flickr_api(creds)

    print_capabilities()

    # Attempt 1: flickr.people.getGroups
    resp1 = try_people_get_groups(user_nsid)
    if args.verbose:
        print("\nRaw people.getGroups:", json.dumps(resp1, indent=2, default=str))
    groups1 = normalize_groups(resp1) if resp1 is not None else []

    # Attempt 2: Person.getGroups via objects
    resp2 = try_person_object_groups(user_nsid)
    if args.verbose:
        print("\nRaw Person.getGroups:", json.dumps(resp2, indent=2, default=str))
    groups2 = normalize_groups(resp2) if resp2 is not None else []

    # Attempt 3: flickr.groups.pools.getGroups (REST)
    resp3 = try_pools_get_groups()
    if args.verbose:
        print("\nRaw groups.pools.getGroups (REST):", json.dumps(resp3, indent=2, default=str))
    groups3 = normalize_groups(resp3) if resp3 is not None else []

    # Attempt 4: groups_pools.getGroups via flickr_api module
    resp4 = try_groups_pools_module_get_groups()
    if args.verbose:
        print("\nRaw groups_pools.getGroups (module):", json.dumps(resp4, indent=2, default=str))
    groups4 = normalize_groups(resp4) if resp4 is not None else []

    # Summaries
    print("\nSummary:")
    print(f"- people.getGroups: {len(groups1)} groups")
    print(f"- Person.getGroups: {len(groups2)} groups")
    print(f"- groups.pools.getGroups (REST): {len(groups3)} groups")
    print(f"- groups_pools.getGroups (module): {len(groups4)} groups")

    # Print one of the lists (prefer people.getGroups)
    final = groups1 or groups2 or groups3 or groups4
    if not final:
        print("\nNo groups found via any method.")
        return 1

    print(f"\nGroups ({len(final)}):")
    for g in final:
        print(f"{g['id']}\t{g['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
