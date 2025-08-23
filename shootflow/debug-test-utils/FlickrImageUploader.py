# Python
#!/usr/bin/env python3
import argparse
import sys
import json
from pathlib import Path

from shootflow.flickr import FlickrAPIClient


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Upload an image to Flickr using configured credentials."
    )
    p.add_argument("file", help="Path to the image file to upload")
    p.add_argument("--title", default=None, help="Title for the photo")
    p.add_argument("--description", default=None, help="Description for the photo")
    p.add_argument(
        "--tags",
        default=None,
        help="Space- or comma-separated tags (e.g. 'tag1,tag2' or 'tag1 tag2')",
    )

    # Visibility flags (bools are accepted by FlickrAPIClient)
    vis = p.add_argument_group("visibility")
    vis.add_argument("--public", dest="is_public", action="store_true", help="Mark as public")
    vis.add_argument("--private", dest="is_public", action="store_false", help="Mark as private (default)")
    vis.set_defaults(is_public=False)

    rel = p.add_argument_group("relationships")
    rel.add_argument("--friend", dest="is_friend", action="store_true", help="Visible to friends")
    rel.add_argument("--no-friend", dest="is_friend", action="store_false", help="Not visible to friends (default)")
    rel.set_defaults(is_friend=False)

    fam = p.add_argument_group("family")
    fam.add_argument("--family", dest="is_family", action="store_true", help="Visible to family")
    fam.add_argument("--no-family", dest="is_family", action="store_false", help="Not visible to family (default)")
    fam.set_defaults(is_family=False)

    # Optional numeric fields
    p.add_argument("--safety-level", type=int, choices=[1, 2, 3], default=None, help="1=safe, 2=moderate, 3=restricted")
    p.add_argument("--content-type", type=int, choices=[1, 2, 3], default=None, help="1=photo, 2=screenshot, 3=other")
    p.add_argument("--hidden", type=int, choices=[1, 2], default=None, help="1=hidden from public searches, 2=visible")

    return p.parse_args(argv)


def parse_tags(raw):
    if not raw:
        return None
    # Accept either comma or space separated; normalize to Flickr's expected space-delimited string
    if "," in raw:
        parts = [t.strip() for t in raw.split(",")]
    else:
        parts = [t.strip() for t in raw.split()]
    tags = [t for t in parts if t]
    return " ".join(tags) if tags else None


def main(argv=None) -> int:
    args = parse_args(argv)

    # Validate file exists before making API calls
    img_path = Path(args.file)
    if not img_path.is_file():
        print(f"Error: File not found: {img_path}", file=sys.stderr)
        return 2

    tags = parse_tags(args.tags)

    client = FlickrAPIClient()  # credentials loaded from settings/Secrets Manager


    photo_id = client.upload_file(
        str(img_path),
        title=args.title,
        description=args.description,
        tags=tags,
        is_public=args.is_public,
        is_friend=args.is_friend,
        is_family=args.is_family,
        safety_level=args.safety_level,
        content_type=args.content_type,
        hidden=args.hidden,
    )



    print(photo_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
