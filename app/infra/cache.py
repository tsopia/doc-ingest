import threading
import time
from collections import OrderedDict
from typing import Optional, Sequence

from app.config import get_settings

_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_cache_lock = threading.Lock()


def cache_ttl_seconds() -> int:
    return get_settings().cache.ttl_seconds


def cache_max_bytes() -> int:
    return get_settings().cache.max_bytes


def cache_key(
    prefix: str,
    identity: str,
    structured: Optional[Sequence[str]],
    keep_data_uris: bool,
    extract_images: bool,
) -> str:
    structured_key = ",".join(sorted(structured or []))
    return (
        f"{prefix}:{identity}|structured={structured_key}|"
        f"keep={int(keep_data_uris)}|extract={int(extract_images)}"
    )


def cache_get(key: str) -> Optional[dict]:
    settings = get_settings().cache
    if not settings.enabled or settings.ttl_seconds <= 0 or settings.max_entries <= 0:
        return None
    now = time.monotonic()
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires_at, data = item
        if expires_at < now:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return data


def cache_set(key: str, data: dict) -> None:
    settings = get_settings().cache
    if not settings.enabled or settings.ttl_seconds <= 0 or settings.max_entries <= 0:
        return
    expires_at = time.monotonic() + settings.ttl_seconds
    with _cache_lock:
        _cache[key] = (expires_at, data)
        _cache.move_to_end(key)
        while len(_cache) > settings.max_entries:
            _cache.popitem(last=False)
