"""S3-compatible storage provider implementation (S3, MinIO, COS)."""

from datetime import timedelta
from minio import Minio
from loguru import logger

from app.infra.storage.base import AbstractStorage


class S3CompatibleStorage(AbstractStorage):
    """S3-compatible storage implementation for AWS S3, MinIO, Tencent COS."""

    def __init__(
        self,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
        bucket: str,
        region: str = "",
        secure: bool = True,
    ):
        """
        Initialize S3-compatible client.

        Args:
            endpoint: S3 endpoint URL (without http/https prefix)
            access_key_id: Access key ID
            access_key_secret: Access key secret
            bucket: Bucket name
            region: Region name (optional, required for some providers)
            secure: Whether to use HTTPS (default: True)
        """
        self.bucket_name = bucket
        self.region = region
        self.secure = secure

        # MinIO client expects endpoint without scheme
        # Remove http:// or https:// if present
        clean_endpoint = endpoint.replace("https://", "").replace("http://", "")

        self.client = Minio(
            clean_endpoint,
            access_key=access_key_id,
            secret_key=access_key_secret,
            secure=secure,
            region=region if region else None,
        )

        logger.debug(
            "S3CompatibleStorage initialized: endpoint={} bucket={} region={} secure={}",
            clean_endpoint,
            bucket,
            region,
            secure,
        )

    def upload_file(self, key: str, data: bytes, mime: str) -> str:
        """
        Upload file to S3-compatible storage.

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
            from io import BytesIO

            data_stream = BytesIO(data)
            self.client.put_object(
                self.bucket_name,
                key,
                data_stream,
                length=len(data),
                content_type=mime,
            )
            logger.debug(
                "S3CompatibleStorage uploaded: key={} size={} mime={}",
                key,
                len(data),
                mime,
            )
            return key
        except Exception as e:
            logger.exception("S3CompatibleStorage upload failed: key={} error={}", key, e)
            raise

    def sign_url(self, key: str, expires_in: int) -> str:
        """
        Generate presigned URL for S3-compatible object.

        Args:
            key: Object key
            expires_in: Expiration time in seconds

        Returns:
            Presigned URL

        Raises:
            Exception: If signing fails
        """
        try:
            url = self.client.presigned_get_object(
                self.bucket_name,
                key,
                expires=timedelta(seconds=expires_in),
            )
            logger.debug(
                "S3CompatibleStorage signed URL: key={} expires_in={}",
                key,
                expires_in,
            )
            return url
        except Exception as e:
            logger.exception("S3CompatibleStorage sign_url failed: key={} error={}", key, e)
            raise

    def exists(self, key: str) -> bool:
        """
        Check if object exists in S3-compatible storage.

        Args:
            key: Object key

        Returns:
            True if exists, False otherwise
        """
        try:
            self.client.stat_object(self.bucket_name, key)
            return True
        except Exception:
            # stat_object raises exception if object doesn't exist
            return False
