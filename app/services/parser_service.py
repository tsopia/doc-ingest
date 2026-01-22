import gc
import hashlib
import io
import os
import shutil
import tempfile
import time
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

from app.config import get_settings
from app.infra.downloader import fetch
from app.infra.storage_client import storage_enabled, upload_images_concurrently
from app.services.model_service import ModelService
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
        self._model_service = ModelService()

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
        overall_start = time.monotonic()
        keep_data_uris = self._normalize_options(keep_data_uris, extract_images)
        settings = get_settings().model

        # Using NamedTemporaryFile to buffer download to disk to avoid OOM
        # delete=True ensures it is removed when closed
        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            try:
                logger.info(
                    "parse_url: START url={} structured={} keep_data_uris={} extract_images={}",
                    url,
                    structured,
                    keep_data_uris,
                    extract_images,
                )
                download_start = time.monotonic()
                with fetch(url) as response:
                    response.raise_for_status()
                    stream_info = stream_info_from_url(url, response.headers)
                    logger.debug(
                        "parse_url: stream_info url={} filename={} extension={} mimetype={} charset={}",
                        url,
                        stream_info.filename,
                        stream_info.extension,
                        stream_info.mimetype,
                        stream_info.charset,
                    )

                    # Stream download to disk
                    shutil.copyfileobj(response.raw, tmp_file)
                    tmp_file.flush()
                    tmp_file.seek(0)
                    download_elapsed = time.monotonic() - download_start
                    logger.info(
                        "parse_url: download done url={} elapsed_ms={}",
                        url,
                        int(download_elapsed * 1000),
                    )

                    # Determine if we need to read file content for single-image fallback
                    # If it's a direct image, we might need bytes ONLY IF extraction fails or we treat it as single image.
                    # But for now let's just convert.
                    # If fallback is needed (extract_images=True but output has no images and input IS image),
                    # we can read from tmp_file then.

                    convert_start = time.monotonic()
                    # MarkItDown typically takes a file-like object or path. check implementation.
                    # If we pass the file object (tmp_file), it should work.
                    # Magika requires a true BinaryIO, _TemporaryFileWrapper might not satisfy type checks.
                    # Open the file path explicitly to get a standard file object.
                    with open(tmp_file.name, "rb") as f_read:
                        result = self._md.convert_stream(
                           f_read,
                            stream_info=stream_info,
                            keep_data_uris=keep_data_uris,
                        )
                    convert_elapsed = time.monotonic() - convert_start
                    logger.info(
                        "parse_url: convert done url={} elapsed_ms={}",
                        url,
                        int(convert_elapsed * 1000),
                    )

                # 内存优化: 释放 MarkItDown 结果对象
                markdown = normalize_markdown(result.text_content or "")
                del result
                images: list[dict] = []

                if extract_images:
                    extract_start = time.monotonic()
                    markdown, images = extract_images_from_markdown(markdown)

                    # Fallback logic: input is image but no images extracted (e.g. OCR fail or just single image input)
                    if not images and is_image_stream(stream_info):
                        with open(tmp_file.name, "rb") as f_read:
                            image_bytes = f_read.read()
                        markdown, images = append_image_from_bytes(
                            markdown, image_bytes, stream_info
                        )
                        logger.debug(
                            "parse_url: fallback image_bytes used url={} images={}",
                            url,
                            len(images),
                        )

                    extract_elapsed = time.monotonic() - extract_start
                    logger.info(
                        "parse_url: extract_images done url={} images={} elapsed_ms={}",
                        url,
                        len(images),
                        int(extract_elapsed * 1000),
                    )

                    if images:
                        # Upload images if storage is enabled
                        if storage_enabled():
                            try:
                                upload_start = time.monotonic()
                                upload_images_concurrently(images)
                                upload_elapsed = time.monotonic() - upload_start
                                logger.info(
                                    "parse_url: upload_images done url={} images={} elapsed_ms={}",
                                    url,
                                    len(images),
                                    int(upload_elapsed * 1000),
                                )
                            except Exception as e:
                                logger.error(f"parse_url: storage upload failed: {e}")
                                data = {"markdown": markdown, "msg": str(e)}
                                if extract_images:
                                    data["images"] = images
                                return data

                data = {"markdown": markdown}

                # Model Processing
                if settings.api_key:
                    try:
                        model_start = time.monotonic()
                        model_result = self._model_service.process_document(markdown, images)
                        model_elapsed = time.monotonic() - model_start
                        if model_result:
                            data["markdown"] = model_result
                            # If model processed it, we don't return raw images
                            logger.info(
                                "parse_url: model parsed url={} length={} elapsed_ms={}",
                                url,
                                len(model_result),
                                int(model_elapsed * 1000),
                            )
                        else:
                             if extract_images:
                                  data["images"] = images
                    except Exception as e:
                        logger.error(f"parse_url: model processing failed with error: {e}")
                        data["msg"] = str(e)
                        # Fallback: keep original markdown and images
                        if extract_images:
                            data["images"] = images

                # Check for OSS errors (bubbled up during upload)
                # Wait, upload_images_concurrently does NOT return errors, it logs them.
                # I need to update upload_images_concurrently to raise exception if any worker failed?
                # The user wants "OSS出错... 把结果给到用户".
                # If upload_images_concurrently raises, it's caught outside.
                # But data is already constructed partially.
                # Let's verify 'upload_images_concurrently' behavior first.


                if structured:
                    struct_start = time.monotonic()
                    data["structured"] = build_structured(data["markdown"], structured)
                    struct_elapsed = time.monotonic() - struct_start
                    logger.info(
                        "parse_url: structured done url={} elapsed_ms={}",
                        url,
                        int(struct_elapsed * 1000),
                    )

                overall_elapsed = time.monotonic() - overall_start
                logger.info(
                    "parse_url: COMPLETE url={} total_elapsed_ms={} chars={} images={}",
                    url,
                    int(overall_elapsed * 1000),
                    len(markdown),
                    len(images),
                )

                # 内存优化: 请求完成后主动触发 GC
                gc.collect()
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
        overall_start = time.monotonic()
        keep_data_uris = self._normalize_options(keep_data_uris, extract_images)
        settings = get_settings().model

        logger.debug(
            "parse_file: metadata filename={} content_type={}",
            file.filename,
            file.content_type,
        )

        # Using NamedTemporaryFile to buffer upload to disk to avoid OOM
        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            try:
                logger.info(
                    "parse_file: START filename={} content_type={} keep_data_uris={} extract_images={}",
                    file.filename,
                    file.content_type,
                    keep_data_uris,
                    extract_images,
                )

                # Copy uploaded file content to disk temp file
                # If file.file is SpooledTemporaryFile, we can just copy it.
                # If it's already rolled over to disk, using its underlying file might be cleaner but
                # copying to our own temp file guarantees we control the lifecycle and path behavior.
                file.file.seek(0)
                shutil.copyfileobj(file.file, tmp_file)
                tmp_file.flush()
                tmp_file.seek(0)

                convert_start = time.monotonic()
                stream_info = StreamInfo(
                    filename=file.filename,
                    extension=os.path.splitext(file.filename)[1] or None,
                    mimetype=file.content_type,
                )
                logger.debug(
                    "parse_file: stream_info filename={} extension={} mimetype={}",
                    stream_info.filename,
                    stream_info.extension,
                    stream_info.mimetype,
                )

                # Open explicit file handle for conversion
                with open(tmp_file.name, "rb") as f_read:
                    result = self._md.convert_stream(
                        f_read,
                        stream_info=stream_info,
                        keep_data_uris=keep_data_uris,
                    )
                convert_elapsed = time.monotonic() - convert_start
                logger.info(
                    "parse_file: convert done filename={} elapsed_ms={}",
                    file.filename,
                    int(convert_elapsed * 1000),
                )

                # 内存优化: 释放 MarkItDown 结果对象
                markdown = normalize_markdown(result.text_content or "")
                del result
                images: list[dict] = []

                if extract_images:
                    extract_start = time.monotonic()
                    markdown, images = extract_images_from_markdown(markdown)

                    if not images and is_image_stream(stream_info):
                        with open(tmp_file.name, "rb") as f_read:
                            image_bytes = f_read.read()
                        markdown, images = append_image_from_bytes(
                            markdown, image_bytes, stream_info
                        )
                        logger.debug(
                            "parse_file: fallback image_bytes used filename={} images={}",
                            file.filename,
                            len(images),
                        )

                    extract_elapsed = time.monotonic() - extract_start
                    logger.info(
                        "parse_file: extract_images done filename={} images={} elapsed_ms={}",
                        file.filename,
                        len(images),
                        int(extract_elapsed * 1000),
                    )

                    if images:
                         if storage_enabled():
                            try:
                                upload_start = time.monotonic()
                                upload_images_concurrently(images)
                                upload_elapsed = time.monotonic() - upload_start
                                logger.info(
                                    "parse_file: upload_images done filename={} images={} elapsed_ms={}",
                                    file.filename,
                                    len(images),
                                    int(upload_elapsed * 1000),
                                )
                            except Exception as e:
                                logger.error(f"parse_file: storage upload failed: {e}")
                                data = {"markdown": markdown, "msg": str(e)}
                                if extract_images:
                                    data["images"] = images
                                return data

                data = {"markdown": markdown}

                # Model Processing
                if settings.api_key:
                    try:
                        model_start = time.monotonic()
                        model_result = self._model_service.process_document(markdown, images)
                        model_elapsed = time.monotonic() - model_start
                        if model_result:
                            data["markdown"] = model_result
                             # If model processed it, we don't return raw images
                            logger.info(
                                "parse_file: model parsed filename={} length={} elapsed_ms={}",
                                file.filename,
                                len(model_result),
                                int(model_elapsed * 1000),
                            )
                        else:
                             if extract_images:
                                data["images"] = images
                    except Exception as e:
                        logger.error(f"parse_file: model processing failed with error: {e}")
                        data["msg"] = str(e)
                        if extract_images:
                            data["images"] = images

                if structured:
                    struct_start = time.monotonic()
                    data["structured"] = build_structured(data["markdown"], structured)
                    struct_elapsed = time.monotonic() - struct_start
                    logger.info(
                        "parse_file: structured done filename={} elapsed_ms={}",
                        file.filename,
                        int(struct_elapsed * 1000),
                    )

                overall_elapsed = time.monotonic() - overall_start
                logger.info(
                    "parse_file: COMPLETE filename={} total_elapsed_ms={} chars={} images={}",
                    file.filename,
                    int(overall_elapsed * 1000),
                    len(markdown),
                    len(images),
                )

                # 内存优化: 请求完成后主动触发 GC
                gc.collect()
                return data

            except (UnsupportedFormatException, FileConversionException, MarkItDownException) as exc:
                logger.exception("parse_file: conversion failed filename={}", file.filename)
                raise ParserServiceError(f"conversion failed: {exc}") from exc
            except Exception as exc:
                logger.exception("parse_file: unknown error filename={}", file.filename)
                raise ParserServiceError(f"unknown error: {exc}") from exc
