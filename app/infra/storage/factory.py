"""Storage factory for creating storage provider instances."""

from typing import Optional

from loguru import logger

from app.config import get_settings
from app.infra.storage.base import AbstractStorage
from app.infra.storage.providers.aliyun import AliyunStorage
from app.infra.storage.providers.s3_compatible import S3CompatibleStorage


def create_storage() -> Optional[AbstractStorage]:
    """
    Create a storage provider instance based on configuration.

    Returns:
        Storage provider instance or None if storage is not configured

    Raises:
        ValueError: If provider is unknown or configuration is invalid
    """
    settings = get_settings().storage

    # Check if storage is configured
    if not all([settings.endpoint, settings.access_key_id, settings.access_key_secret, settings.bucket]):
        logger.debug("Storage not configured, skipping storage creation")
        return None

    provider = settings.provider.lower()

    if provider == "oss":
        logger.info("Creating Aliyun OSS storage provider")
        return AliyunStorage(
            endpoint=settings.endpoint,
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
            bucket=settings.bucket,
            secure=settings.secure,
        )
    elif provider == "cos":
        from app.infra.storage.providers.tencent_cos import TencentCOSStorage

        if not settings.region:
            raise ValueError("Region is required for Tencent Cloud COS")
        logger.info("Creating Tencent Cloud COS storage provider")
        return TencentCOSStorage(
            endpoint=settings.endpoint,
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
            bucket=settings.bucket,
            region=settings.region,
            secure=settings.secure,
        )
    elif provider in ["s3", "minio"]:
        logger.info(f"Creating S3-compatible storage provider: {provider}")
        return S3CompatibleStorage(
            endpoint=settings.endpoint,
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
            bucket=settings.bucket,
            region=settings.region,
            secure=settings.secure,
        )
    else:
        raise ValueError(
            f"Unknown storage provider: {provider}. "
            f"Supported providers: oss, s3, minio, cos"
        )
