import hashlib
import io
import os
from typing import Optional, Sequence

import requests
from loguru import logger
from markitdown import (
    FileConversionException,
    MarkItDown,
    MarkItDownException,
    StreamInfo,
    UnsupportedFormatException,
)

from app.infra.cache import cache_get, cache_key, cache_max_bytes, cache_set, cache_ttl_seconds
from app.infra.downloader import fetch
from app.infra.oss_client import oss_enabled, oss_url_ttl_seconds
from app.utils.parse_utils import (
    append_image_from_bytes,
    build_structured,
    extract_images_from_markdown,
    is_image_stream,
    normalize_markdown,
    stream_info_from_url,
)


class ParserServiceError(Exception):
    pass


class ParserService:
    def __init__(self, md: Optional[MarkItDown] = None) -> None:
        self._md = md or MarkItDown()

    @staticmethod
    def _sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _normalize_options(self, keep_data_uris: bool, extract_images: bool) -> bool:
        if extract_images and not keep_data_uris:
            logger.warning("keep_data_uris overridden to True for extraction")
            return True
        return keep_data_uris

    def parse_url(
        self,
        *,
        url: str,
        structured: Optional[Sequence[str]],
        keep_data_uris: bool,
        extract_images: bool,
    ) -> dict:
        keep_data_uris = self._normalize_options(keep_data_uris, extract_images)

        cache_key_value = None
        if not (
            oss_enabled()
            and extract_images
            and cache_ttl_seconds() > oss_url_ttl_seconds()
        ):
            cache_key_value = cache_key(
                "url", url, structured, keep_data_uris, extract_images
            )
            cached = cache_get(cache_key_value)
            if cached is not None:
                logger.info("parse_url: cache hit url={}", url)
                return cached

        try:
            logger.info(
                "parse_url: downloading {} structured={} keep_data_uris={} extract_images={}",
                url,
                structured,
                keep_data_uris,
                extract_images,
            )
            with fetch(url) as response:
                response.raise_for_status()
                stream_info = stream_info_from_url(url, response.headers)
                image_bytes = None
                file_stream = response.raw
                content_length = response.headers.get("Content-Length")
                cacheable = False
                if content_length and content_length.isdigit():
                    cacheable = int(content_length) <= cache_max_bytes()
                if cacheable:
                    body = response.content
                    file_stream = io.BytesIO(body)
                    if extract_images and is_image_stream(stream_info):
                        image_bytes = body
                elif extract_images and is_image_stream(stream_info):
                    image_bytes = response.content
                    file_stream = io.BytesIO(image_bytes)
                if image_bytes is not None and not cacheable:
                    cacheable = len(image_bytes) <= cache_max_bytes()
                result = self._md.convert_stream(
                    file_stream,
                    stream_info=stream_info,
                    keep_data_uris=keep_data_uris,
                )

            markdown = normalize_markdown(result.text_content or "")
            images: list[dict] = []
            if extract_images:
                markdown, images = extract_images_from_markdown(markdown)
                if not images and image_bytes is not None:
                    markdown, images = append_image_from_bytes(
                        markdown, image_bytes, stream_info
                    )
            data = {"markdown": markdown}
            if structured:
                data["structured"] = build_structured(markdown, structured)
            if extract_images:
                data["images"] = images
            if cacheable and cache_key_value is not None:
                cache_set(cache_key_value, data)
            logger.info(
                "parse_url: success url={} chars={} structured={} images={}",
                url,
                len(markdown),
                structured,
                len(images),
            )
            return data
        except requests.RequestException as exc:
            logger.exception("parse_url: download failed url={}", url)
            raise ParserServiceError(f"download failed: {exc}") from exc
        except (UnsupportedFormatException, FileConversionException, MarkItDownException) as exc:
            logger.exception("parse_url: conversion failed url={}", url)
            raise ParserServiceError(f"conversion failed: {exc}") from exc
        except Exception as exc:
            logger.exception("parse_url: unknown error url={}", url)
            raise ParserServiceError(f"unknown error: {exc}") from exc

    def parse_file(
        self,
        *,
        file,
        structured: Optional[Sequence[str]],
        keep_data_uris: bool,
        extract_images: bool,
    ) -> dict:
        keep_data_uris = self._normalize_options(keep_data_uris, extract_images)

        cache_key_value = None
        file_bytes = None
        file_size = None
        try:
            file.file.seek(0, io.SEEK_END)
            file_size = file.file.tell()
            file.file.seek(0)
        except Exception:
            file_size = None
        if file_size is not None and file_size <= cache_max_bytes():
            file_bytes = file.file.read()
            file.file.seek(0)
            digest = self._sha256_bytes(file_bytes)
            if not (
                oss_enabled()
                and extract_images
                and cache_ttl_seconds() > oss_url_ttl_seconds()
            ):
                cache_key_value = cache_key(
                    "file", digest, structured, keep_data_uris, extract_images
                )
                cached = cache_get(cache_key_value)
                if cached is not None:
                    logger.info("parse_file: cache hit filename={}", file.filename)
                    return cached

        try:
            logger.info(
                "parse_file: received filename={} content_type={} keep_data_uris={} extract_images={}",
                file.filename,
                file.content_type,
                keep_data_uris,
                extract_images,
            )
            file.file.seek(0)
            stream_info = StreamInfo(
                filename=file.filename,
                extension=os.path.splitext(file.filename)[1] or None,
                mimetype=file.content_type,
            )
            image_bytes = None
            file_stream = file.file
            if file_bytes is not None:
                file_stream = io.BytesIO(file_bytes)
            if extract_images and is_image_stream(stream_info):
                if file_bytes is None:
                    image_bytes = file.file.read()
                    file.file.seek(0)
                else:
                    image_bytes = file_bytes
            result = self._md.convert_stream(
                file_stream,
                stream_info=stream_info,
                keep_data_uris=keep_data_uris,
            )
            markdown = normalize_markdown(result.text_content or "")
            images: list[dict] = []
            if extract_images:
                markdown, images = extract_images_from_markdown(markdown)
                if not images and image_bytes is not None:
                    markdown, images = append_image_from_bytes(
                        markdown, image_bytes, stream_info
                    )
            data = {"markdown": markdown}
            if structured:
                data["structured"] = build_structured(markdown, structured)
            if extract_images:
                data["images"] = images
            if cache_key_value is not None:
                cache_set(cache_key_value, data)
            logger.info(
                "parse_file: success filename={} chars={} structured={} images={}",
                file.filename,
                len(markdown),
                structured,
                len(images),
            )
            return data
        except (UnsupportedFormatException, FileConversionException, MarkItDownException) as exc:
            logger.exception("parse_file: conversion failed filename={}", file.filename)
            raise ParserServiceError(f"conversion failed: {exc}") from exc
        except Exception as exc:
            logger.exception("parse_file: unknown error filename={}", file.filename)
            raise ParserServiceError(f"unknown error: {exc}") from exc
