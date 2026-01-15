import threading
import time
from collections import OrderedDict
from typing import Optional, Sequence

from app.config import get_settings

_cache: "OrderedDict[str, tuple[float, dict, int]]" = OrderedDict()
_cache_bytes = 0
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


def _estimate_size(value: object) -> int:
    # Approximate size to enforce a global cache budget.
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (int, float, bool)):
        return 8
    if isinstance(value, dict):
        total = 0
        for k, v in value.items():
            total += _estimate_size(k)
            total += _estimate_size(v)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_estimate_size(item) for item in value)
    return 0


def _purge_expired(now: float) -> None:
    global _cache_bytes
    if not _cache:
        return
    expired_keys = []
    for key, (expires_at, _, size) in _cache.items():
        if expires_at < now:
            expired_keys.append((key, size))
    if not expired_keys:
        return
    for key, size in expired_keys:
        _cache.pop(key, None)
        _cache_bytes -= size


def cache_get(key: str) -> Optional[dict]:
    settings = get_settings().cache
    if not settings.enabled or settings.ttl_seconds <= 0 or settings.max_entries <= 0:
        return None
    now = time.monotonic()
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        expires_at, data, size = item
        if expires_at < now:
            _cache.pop(key, None)
            global _cache_bytes
            _cache_bytes -= size
            return None
        _cache.move_to_end(key)
        return data


def cache_set(key: str, data: dict) -> None:
    settings = get_settings().cache
    if not settings.enabled or settings.ttl_seconds <= 0 or settings.max_entries <= 0:
        return
    entry_size = _estimate_size(data)
    if settings.max_bytes > 0 and entry_size > settings.max_bytes:
        return
    expires_at = time.monotonic() + settings.ttl_seconds
    with _cache_lock:
        _purge_expired(time.monotonic())
        global _cache_bytes
        existing = _cache.get(key)
        if existing:
            _cache_bytes -= existing[2]
        _cache[key] = (expires_at, data, entry_size)
        _cache_bytes += entry_size
        _cache.move_to_end(key)
        if settings.max_total_bytes > 0:
            while _cache and _cache_bytes > settings.max_total_bytes:
                _, (_, _, size) = _cache.popitem(last=False)
                _cache_bytes -= size
        while len(_cache) > settings.max_entries:
            _, (_, _, size) = _cache.popitem(last=False)
            _cache_bytes -= size
