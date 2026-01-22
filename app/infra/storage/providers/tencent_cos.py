"""Tencent Cloud COS storage provider implementation."""

from qcloud_cos import CosConfig, CosS3Client
from loguru import logger

from app.infra.storage.base import AbstractStorage


class TencentCOSStorage(AbstractStorage):
    """Tencent Cloud COS storage implementation."""

    def __init__(
        self,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
        bucket: str,
        region: str,
        secure: bool = True,
    ):
        """
        Initialize Tencent Cloud COS client.

        Args:
            endpoint: COS endpoint (e.g., cos.ap-guangzhou.myqcloud.com)
            access_key_id: SecretId
            access_key_secret: SecretKey
            bucket: Bucket name
            region: Region (e.g., ap-guangzhou, ap-shanghai)
            secure: Whether to use HTTPS (default: True)
        """
        self.bucket_name = bucket
        self.region = region
        self.secure = secure

        # COS Config
        scheme = "https" if secure else "http"
        config = CosConfig(
            Region=region,
            SecretId=access_key_id,
            SecretKey=access_key_secret,
            Scheme=scheme,
        )

        self.client = CosS3Client(config)

        logger.debug(
            "TencentCOSStorage initialized: region={} bucket={} secure={}",
            region,
            bucket,
            secure,
        )

    def upload_file(self, key: str, data: bytes, mime: str) -> str:
        """
        Upload file to Tencent Cloud COS.

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
                Bucket=self.bucket_name,
                Body=data_stream,
                Key=key,
                ContentType=mime,
            )
            logger.debug(
                "TencentCOSStorage uploaded: key={} size={} mime={}",
                key,
                len(data),
                mime,
            )
            return key
        except Exception as e:
            logger.exception("TencentCOSStorage upload failed: key={} error={}", key, e)
            raise

    def sign_url(self, key: str, expires_in: int) -> str:
        """
        Generate presigned URL for COS object.

        Args:
            key: Object key
            expires_in: Expiration time in seconds

        Returns:
            Presigned URL

        Raises:
            Exception: If signing fails
        """
        try:
            url = self.client.get_presigned_url(
                Method="GET",
                Bucket=self.bucket_name,
                Key=key,
                Expired=expires_in,
            )
            logger.debug(
                "TencentCOSStorage signed URL: key={} expires_in={}",
                key,
                expires_in,
            )
            return url
        except Exception as e:
            logger.exception("TencentCOSStorage sign_url failed: key={} error={}", key, e)
            raise

    def exists(self, key: str) -> bool:
        """
        Check if object exists in Tencent Cloud COS.

        Args:
            key: Object key

        Returns:
            True if exists, False otherwise
        """
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception:
            # head_object raises exception if object doesn't exist
            return False
