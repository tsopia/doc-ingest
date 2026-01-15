import base64
import mimetypes
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import oss2
from loguru import logger

from app.config import get_settings

_oss_bucket: Optional[oss2.Bucket] = None
_oss_lock = threading.Lock()
_UPLOAD_THREADS = 10
_UPLOAD_RETRIES = 3


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


def _upload_image(image: dict, bucket: oss2.Bucket, oss, retries: int) -> bool:
    base64_data = image.get("base64")
    mime = image.get("mime", "application/octet-stream")
    if not base64_data:
        logger.debug("oss upload skipped: missing base64 id={}", image.get("id"))
        return False
    try:
        data = base64.b64decode(base64_data, validate=True)
    except Exception:
        logger.debug("oss upload skipped: invalid base64 id={}", image.get("id"))
        return False
    object_name = uuid.uuid4().hex
    key = _object_key(object_name, mime)
    attempts = max(1, retries)
    start = time.monotonic()
    logger.debug(
        "oss upload start id={} key={} size_bytes={} mime={} attempts={}",
        image.get("id"),
        key,
        len(data),
        mime,
        attempts,
    )
    for attempt in range(attempts):
        try:
            if attempt:
                logger.debug(
                    "oss upload retry id={} key={} attempt={}",
                    image.get("id"),
                    key,
                    attempt + 1,
                )
            bucket.put_object(key, data)
            url = bucket.sign_url("GET", key, oss.url_ttl_seconds, slash_safe=True)
            if not oss.secure:
                url = url.replace("https://", "http://", 1)
            image["url"] = url
            image["url_expires_in"] = oss.url_ttl_seconds
            image.pop("base64", None)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.debug(
                "oss upload success id={} key={} elapsed_ms={}",
                image.get("id"),
                key,
                elapsed_ms,
            )
            return True
        except Exception as exc:
            if attempt == attempts - 1:
                logger.exception("oss upload failed error={}", exc)
    return False


def attach_oss_url(image: dict) -> None:
    bucket = _get_bucket()
    if bucket is None:
        return
    oss = get_settings().oss
    _upload_image(image, bucket, oss, _UPLOAD_RETRIES)


def upload_images_concurrently(images: list[dict]) -> None:
    if not images:
        return
    bucket = _get_bucket()
    if bucket is None:
        return
    oss = get_settings().oss
    max_workers = min(_UPLOAD_THREADS, len(images))
    logger.debug(
        "oss upload batch start images={} max_workers={}",
        len(images),
        max_workers,
    )
    success_count = 0
    if max_workers <= 1:
        for image in images:
            if _upload_image(image, bucket, oss, _UPLOAD_RETRIES):
                success_count += 1
        logger.debug(
            "oss upload batch done images={} success={} failed={}",
            len(images),
            success_count,
            len(images) - success_count,
        )
        return
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_upload_image, image, bucket, oss, _UPLOAD_RETRIES): image
            for image in images
        }
        for future in futures:
            try:
                if future.result():
                    success_count += 1
            except Exception as exc:
                logger.exception("oss upload worker failed error={}", exc)
        logger.debug(
            "oss upload batch done images={} success={} failed={}",
            len(images),
            success_count,
            len(images) - success_count,
        )
