from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "DOC_INGEST__"


class CacheSettings(BaseModel):
    enabled: bool = Field(
        default=True,
    )
    ttl_seconds: int = Field(
        default=300,
        ge=0,
    )
    max_entries: int = Field(
        default=256,
        ge=0,
    )
    max_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=0,
    )


class OssSettings(BaseModel):
    endpoint: str = Field(
        default="",
    )
    access_key_id: str = Field(
        default="",
    )
    access_key_secret: str = Field(
        default="",
    )
    bucket: str = Field(
        default="",
    )
    prefix: str = Field(
        default="doc-ingest/",
    )
    secure: bool = Field(
        default=True,
    )
    url_ttl_seconds: int = Field(
        default=1800,
        ge=0,
    )


class DownloadSettings(BaseModel):
    timeout_seconds: int = Field(
        default=30,
        ge=1,
    )


class Settings(BaseSettings):
    cache: CacheSettings = CacheSettings()
    oss: OssSettings = OssSettings()
    download: DownloadSettings = DownloadSettings()

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
