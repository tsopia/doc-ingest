import base64
import mimetypes
import threading
import uuid
from typing import Optional

import oss2
from loguru import logger

from app.config import get_settings

_oss_bucket: Optional[oss2.Bucket] = None
_oss_lock = threading.Lock()


def oss_enabled() -> bool:
    oss = get_settings().oss
    return all([oss.endpoint, oss.access_key_id, oss.access_key_secret, oss.bucket])


def oss_url_ttl_seconds() -> int:
    return get_settings().oss.url_ttl_seconds


def _get_bucket() -> Optional[oss2.Bucket]:
    if not oss_enabled():
        return None
    oss = get_settings().oss
    global _oss_bucket
    if _oss_bucket is not None:
        return _oss_bucket
    with _oss_lock:
        if _oss_bucket is not None:
            return _oss_bucket
        auth = oss2.Auth(oss.access_key_id, oss.access_key_secret)
        _oss_bucket = oss2.Bucket(auth, oss.endpoint, oss.bucket, is_cname=False)
        return _oss_bucket


def _object_key(object_name: str, mime: str) -> str:
    extension = mimetypes.guess_extension(mime or "") or ".bin"
    name = f"{object_name}{extension}"
    prefix = get_settings().oss.prefix
    if prefix:
        return f"{prefix.rstrip('/')}/{name}"
    return name


def attach_oss_url(image: dict) -> None:
    bucket = _get_bucket()
    if bucket is None:
        return
    oss = get_settings().oss
    base64_data = image.get("base64")
    mime = image.get("mime", "application/octet-stream")
    if not base64_data:
        return
    try:
        data = base64.b64decode(base64_data, validate=True)
    except Exception:
        return
    object_name = uuid.uuid4().hex
    key = _object_key(object_name, mime)
    try:
        bucket.put_object(key, data)
        url = bucket.sign_url("GET", key, oss.url_ttl_seconds, slash_safe=True)
        if not oss.secure:
            url = url.replace("https://", "http://", 1)
        image["url"] = url
        image["url_expires_in"] = oss.url_ttl_seconds
        image.pop("base64", None)
    except Exception as exc:
        logger.exception("oss upload failed error={}", exc)
