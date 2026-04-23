"""Storage client for uploading images to object storage."""

import base64
import mimetypes
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from loguru import logger

from app.config import get_settings
from app.infra.storage import AbstractStorage, create_storage

_storage_instance: Optional[AbstractStorage] = None
_storage_lock = threading.Lock()
_UPLOAD_THREADS = 10
_UPLOAD_RETRIES = 3


def storage_enabled() -> bool:
    """Check if storage is enabled based on configuration."""
    storage = get_settings().storage
    return all([storage.endpoint, storage.access_key_id, storage.access_key_secret, storage.bucket])


def storage_url_ttl_seconds() -> int:
    """Get the URL TTL in seconds from configuration."""
    return get_settings().storage.url_ttl_seconds


def _get_storage() -> Optional[AbstractStorage]:
    """
    Get or create the storage instance (singleton pattern).

    Returns:
        Storage instance or None if storage is not configured
    """
    if not storage_enabled():
        return None

    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    with _storage_lock:
        if _storage_instance is not None:
            return _storage_instance
        _storage_instance = create_storage()
        return _storage_instance


def _object_key(object_name: str, mime: str) -> str:
    """
    Generate object key with prefix and extension.

    Args:
        object_name: Base object name (usually UUID)
        mime: MIME type for determining extension

    Returns:
        Full object key with prefix
    """
    extension = mimetypes.guess_extension(mime or "") or ".bin"
    name = f"{object_name}{extension}"
    prefix = get_settings().storage.prefix
    if prefix:
        return f"{prefix.rstrip('/')}/{name}"
    return name


def _upload_image(image: dict, storage: AbstractStorage, retries: int) -> bool:
    """
    Upload a single image to storage.

    Args:
        image: Image dictionary with 'base64', 'mime', 'id' fields
        storage: Storage instance
        retries: Number of retry attempts

    Returns:
        True if upload succeeded, False otherwise
    """
    base64_data = image.get("base64")
    mime = image.get("mime", "application/octet-stream")
    if not base64_data:
        logger.debug("storage upload skipped: missing base64 id={}", image.get("id"))
        return False

    try:
        data = base64.b64decode(base64_data, validate=True)
    except Exception:
        logger.debug("storage upload skipped: invalid base64 id={}", image.get("id"))
        return False

    object_name = uuid.uuid4().hex
    key = _object_key(object_name, mime)
    attempts = max(1, retries)
    start = time.monotonic()

    logger.debug(
        "storage upload start id={} key={} size_bytes={} mime={} attempts={}",
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
                    "storage upload retry id={} key={} attempt={}",
                    image.get("id"),
                    key,
                    attempt + 1,
                )

            # Upload file
            storage.upload_file(key, data, mime)

            # Generate signed URL
            url = storage.sign_url(key, storage_url_ttl_seconds())

            # Update image dict
            image["url"] = url
            image["url_expires_in"] = storage_url_ttl_seconds()
            image.pop("base64", None)

            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.debug(
                "storage upload success id={} key={} elapsed_ms={}",
                image.get("id"),
                key,
                elapsed_ms,
            )
            return True
        except Exception as exc:
            if attempt == attempts - 1:
                logger.exception("storage upload failed error={}", exc)
                raise exc
    return False


def attach_storage_url(image: dict) -> None:
    """
    Upload a single image and attach its URL (synchronous).

    Args:
        image: Image dictionary to update with URL
    """
    storage = _get_storage()
    if storage is None:
        return
    _upload_image(image, storage, _UPLOAD_RETRIES)


def upload_images_concurrently(images: list[dict]) -> None:
    """
    Upload multiple images concurrently to storage.

    Args:
        images: List of image dictionaries to upload

    Raises:
        Exception: If any upload fails
    """
    if not images:
        return

    storage = _get_storage()
    if storage is None:
        return

    max_workers = min(_UPLOAD_THREADS, len(images))
    start = time.monotonic()
    logger.info(
        "storage upload batch START images={} max_workers={}",
        len(images),
        max_workers,
    )

    success_count = 0

    if max_workers <= 1:
        for image in images:
            if _upload_image(image, storage, _UPLOAD_RETRIES):
                success_count += 1
        elapsed = time.monotonic() - start
        logger.info(
            "storage upload batch DONE images={} success={} failed={} elapsed_ms={}",
            len(images),
            success_count,
            len(images) - success_count,
            int(elapsed * 1000),
        )
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_upload_image, image, storage, _UPLOAD_RETRIES): image
            for image in images
        }
        failed_count = 0
        for future in futures:
            try:
                if future.result():
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as exc:
                failed_count += 1
                logger.warning("storage upload worker failed error={}", exc)
        elapsed = time.monotonic() - start
        logger.info(
            "storage upload batch DONE images={} success={} failed={} elapsed_ms={}",
            len(images),
            success_count,
            failed_count,
            int(elapsed * 1000),
        )
        if failed_count > 0:
            logger.warning(
                "storage upload: {} image(s) failed to upload, "
                "fallback to base64 for model processing",
                failed_count,
            )

