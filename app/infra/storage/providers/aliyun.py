"""Aliyun OSS storage provider implementation."""

import oss2
from loguru import logger

from app.infra.storage.base import AbstractStorage


class AliyunStorage(AbstractStorage):
    """Aliyun OSS storage implementation."""

    def __init__(
        self,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
        bucket: str,
        secure: bool = True,
    ):
        """
        Initialize Aliyun OSS client.

        Args:
            endpoint: OSS endpoint URL
            access_key_id: Access key ID
            access_key_secret: Access key secret
            bucket: Bucket name
            secure: Whether to use HTTPS (default: True)
        """
        self.endpoint = endpoint
        self.bucket_name = bucket
        self.secure = secure

        auth = oss2.Auth(access_key_id, access_key_secret)
        self.bucket = oss2.Bucket(auth, endpoint, bucket, is_cname=False)

        logger.debug(
            "AliyunStorage initialized: endpoint={} bucket={} secure={}",
            endpoint,
            bucket,
            secure,
        )

    def upload_file(self, key: str, data: bytes, mime: str) -> str:
        """
        Upload file to Aliyun OSS.

        Args:
            key: Object key
            data: File content
            mime: MIME type

        Returns:
            Object key

        Raises:
            Exception: If upload fails
        """
        try:
            self.bucket.put_object(key, data)
            logger.debug(
                "AliyunStorage uploaded: key={} size={} mime={}",
                key,
                len(data),
                mime,
            )
            return key
        except Exception as e:
            logger.exception("AliyunStorage upload failed: key={} error={}", key, e)
            raise

    def sign_url(self, key: str, expires_in: int) -> str:
        """
        Generate signed URL for Aliyun OSS object.

        Args:
            key: Object key
            expires_in: Expiration time in seconds

        Returns:
            Signed URL

        Raises:
            Exception: If signing fails
        """
        try:
            url = self.bucket.sign_url("GET", key, expires_in, slash_safe=True)
            # Convert to HTTP if secure is False
            if not self.secure:
                url = url.replace("https://", "http://", 1)
            logger.debug(
                "AliyunStorage signed URL: key={} expires_in={} secure={}",
                key,
                expires_in,
                self.secure,
            )
            return url
        except Exception as e:
            logger.exception("AliyunStorage sign_url failed: key={} error={}", key, e)
            raise

    def exists(self, key: str) -> bool:
        """
        Check if object exists in Aliyun OSS.

        Args:
            key: Object key

        Returns:
            True if exists, False otherwise
        """
        try:
            return self.bucket.object_exists(key)
        except Exception as e:
            logger.exception("AliyunStorage exists check failed: key={} error={}", key, e)
            return False
