import re
from functools import lru_cache

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "DOC_INGEST__"
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?b?)?\s*$", re.IGNORECASE)


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
    max_total_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=0,
    )

    @field_validator("max_bytes", "max_total_bytes", mode="before")
    @classmethod
    def _parse_size(cls, value: object) -> object:
        if value is None or isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if raw.isdigit():
            return int(raw)
        match = _SIZE_RE.match(raw)
        if not match:
            raise ValueError("invalid size format")
        number = float(match.group(1))
        unit = (match.group(2) or "b").lower()
        multipliers = {
            "b": 1,
            "k": 1024,
            "kb": 1024,
            "m": 1024**2,
            "mb": 1024**2,
            "g": 1024**3,
            "gb": 1024**3,
            "t": 1024**4,
            "tb": 1024**4,
        }
        if unit not in multipliers:
            raise ValueError("invalid size unit")
        return int(number * multipliers[unit])


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


class LogSettings(BaseModel):
    level: str = Field(
        default="INFO",
    )


class Settings(BaseSettings):
    cache: CacheSettings = CacheSettings()
    oss: OssSettings = OssSettings()
    download: DownloadSettings = DownloadSettings()
    log: LogSettings = LogSettings()

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
