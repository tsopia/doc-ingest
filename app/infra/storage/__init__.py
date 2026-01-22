"""Storage package."""

from app.infra.storage.base import AbstractStorage
from app.infra.storage.factory import create_storage

__all__ = ["AbstractStorage", "create_storage"]
