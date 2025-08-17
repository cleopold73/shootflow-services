# language: python
import base64
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

import exiftool  # pyexiftool wrapper

from shootflow.shared.config import settings

logger = logging.getLogger(__name__)


def _load_blocklist() -> set[str]:
    raw = settings.EXIF_BLOCKLIST
    if not raw:
        return set()
    # Accept list/set/tuple or comma-separated string
    if isinstance(raw, str):
        items = [p.strip() for p in raw.split(",")]
    else:
        items = list(raw)
    # Normalize
    return {str(x).strip().lower() for x in items if str(x).strip()}


GEAR_TAG_BLOCKLIST: set[str] = _load_blocklist()

# Tunables (sourced from central settings)
EXIF_MAX_TEXT_LENGTH: int = settings.EXIF_MAX_TEXT_LENGTH
EXIF_BASE64_SNIFF_PREFIX_LEN: int = settings.EXIF_BASE64_SNIFF_PREFIX_LEN
EXIF_LARGE_COLLECTION_SIZE: int = settings.EXIF_LARGE_COLLECTION_SIZE


def _maybe_put(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value not in ("", [], {}):
        target[key] = value


def run_exiftool_json(file_path: str) -> Optional[Dict[str, Any]]:
    """
    Read metadata via pyexiftool using JSON output and -n to ensure numeric values where possible.
    Returns a flat dict with keys that parse_exif_fields understands.
    """
    try:
        with exiftool.ExifTool() as et:
            # Only use execute_json with -n. No fallback or silent suppression.
            results = et.execute_json("-n", file_path)

            if isinstance(results, list) and results:
                md: Dict[str, Any] = dict(results[0])
            elif isinstance(results, dict):
                md = dict(results)
            else:
                logger.warning("Unexpected exiftool JSON result for %s: %r", file_path, type(results))
                raise

        out: Dict[str, Any] = {}

        def g(k: str) -> Any:
            return md.get(k)

        # XMP title/description/subject
        _maybe_put(out, "XMP-dc:Title", g("XMP-dc:Title") or g("XMP:Title"))
        _maybe_put(out, "XMP:Title", g("XMP:Title"))
        _maybe_put(out, "XMP-dc:Description", g("XMP-dc:Description") or g("XMP:Description"))
        _maybe_put(out, "XMP:Description", g("XMP:Description"))
        _maybe_put(out, "XMP-dc:Subject", g("XMP-dc:Subject") or g("XMP:Subject"))

        # IPTC title/keywords
        _maybe_put(out, "IPTC:ObjectName", g("IPTC:ObjectName"))
        _maybe_put(out, "IPTC:Keywords", g("IPTC:Keywords"))
        _maybe_put(out, "Description", g("IPTC:Caption-Abstract") or g("IPTC:Caption"))

        # EXIF title/description/comment
        _maybe_put(out, "ImageDescription", g("EXIF:ImageDescription"))
        _maybe_put(out, "EXIF:ImageDescription", g("EXIF:ImageDescription"))
        _maybe_put(out, "UserComment", g("EXIF:UserComment"))
        _maybe_put(out, "EXIF:UserComment", g("EXIF:UserComment"))

        # GPS (numeric via -n)
        _maybe_put(out, "EXIF:GPSLatitude", g("EXIF:GPSLatitude") or g("GPSLatitude"))
        _maybe_put(out, "EXIF:GPSLongitude", g("EXIF:GPSLongitude") or g("GPSLongitude"))
        _maybe_put(out, "EXIF:GPSAltitude", g("EXIF:GPSAltitude") or g("GPSAltitude"))

        return out
    except Exception as e:
        logger.error("Error running exiftool on %s: %s", file_path, e, exc_info=True)
        return None


def _first_present(meta: Dict[str, Any], keys: Iterable[str]) -> Optional[Any]:
    for k in keys:
        if k in meta and meta[k] not in (None, "", []):
            return meta[k]
    return None


def _ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _normalize_tags(raw_tags: Iterable[str]) -> List[str]:
    dedup: List[str] = []
    seen = set()
    for t in raw_tags:
        t_norm = t.strip().lower()
        if not t_norm:
            continue
        if any(bad in t_norm for bad in GEAR_TAG_BLOCKLIST):
            continue
        if t_norm not in seen:
            seen.add(t_norm)
            dedup.append(t_norm)
    return dedup


def _looks_like_base64(s: str) -> bool:
    prefix = s[:EXIF_BASE64_SNIFF_PREFIX_LEN]
    return all(c.isalnum() or c in "+/=\n\r" for c in prefix)


def _is_large_or_binary_field(key: str, value: Any) -> bool:
    large_like = {"PreviewImage", "ThumbnailImage", "JpgFromRaw", "OtherImage", "MPImage", "MakerNotes"}
    if key in large_like:
        return True
    if isinstance(value, str):
        if len(value) > EXIF_MAX_TEXT_LENGTH:
            return True
        if _looks_like_base64(value) and len(value) > 2048:
            try:
                base64.b64decode(value, validate=True)
                return True
            except Exception:
                pass
    if isinstance(value, (list, dict)) and len(value) > EXIF_LARGE_COLLECTION_SIZE:
        return True
    return False


def parse_exif_fields(
    meta: Dict[str, Any], fallback_title: str
) -> Tuple[Optional[str], Optional[str], List[str], Dict[str, Any]]:
    title = _first_present(
        meta,
        ["XMP-dc:Title", "XMP:Title", "Title", "IPTC:ObjectName", "ImageDescription", "EXIF:ImageDescription"],
    )
    caption = _first_present(
        meta,
        ["XMP-dc:Description", "XMP:Description", "Description", "IPTC:Caption-Abstract", "UserComment", "EXIF:UserComment"],
    )

    # Collect tags from both XMP subjects and IPTC keywords (not just the first present)
    subject_tags = _ensure_list(_first_present(meta, ["XMP-dc:Subject", "XMP:Subject", "Subject"]))
    keyword_tags = _ensure_list(_first_present(meta, ["IPTC:Keywords"]))
    keywords = _normalize_tags([*subject_tags, *keyword_tags])

    # GPS
    lat = _first_present(meta, ["GPSLatitude", "EXIF:GPSLatitude"])
    lon = _first_present(meta, ["GPSLongitude", "EXIF:GPSLongitude"])
    alt = _first_present(meta, ["GPSAltitude", "EXIF:GPSAltitude"])
    gps = None
    try:
        def _to_float(v: Any) -> Optional[float]:
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, (list, tuple)) and len(v) == 3:
                d, m, s = v
                deg = float(d)
                sign = -1.0 if deg < 0 else 1.0
                total = abs(deg) + abs(float(m)) / 60.0 + abs(float(s)) / 3600.0
                return sign * total
            try:
                return float(str(v))
            except Exception:
                return None

        lat_f = _to_float(lat)
        lon_f = _to_float(lon)
        alt_f = _to_float(alt)

        if lat_f is not None and lon_f is not None:
            gps = {"lat": lat_f, "lon": lon_f}
            if alt_f is not None:
                gps["alt"] = alt_f
    except (TypeError, ValueError):
        gps = None

    exif_kept = {k: v for k, v in meta.items() if not _is_large_or_binary_field(k, v)}

    if not title:
        title = fallback_title

    return (
        str(title) if title else None,
        str(caption) if caption else None,
        keywords,
        {"gps": gps, "exif": exif_kept},
    )