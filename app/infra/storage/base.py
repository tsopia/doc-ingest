"""
Abstract base class for object storage providers.
"""

from abc import ABC, abstractmethod
from typing import Optional


class AbstractStorage(ABC):
    """Abstract interface for object storage operations."""

    @abstractmethod
    def upload_file(self, key: str, data: bytes, mime: str) -> str:
        """
        Upload a file to object storage.

        Args:
            key: Object key/path in the storage
            data: File content as bytes
            mime: MIME type of the file

        Returns:
            Object key that was used for storage

        Raises:
            Exception: If upload fails
        """
        pass

    @abstractmethod
    def sign_url(self, key: str, expires_in: int) -> str:
        """
        Generate a signed URL for accessing the object.

        Args:
            key: Object key/path in the storage
            expires_in: URL expiration time in seconds

        Returns:
            Signed URL string

        Raises:
            Exception: If signing fails
        """
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if an object exists in storage.

        Args:
            key: Object key/path in the storage

        Returns:
            True if object exists, False otherwise
        """
        pass
