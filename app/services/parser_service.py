"""Parser service for document conversion."""

import gc
import os
import shutil
import tempfile
import time

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
from app.utils.observability import observe
from app.utils.parse_utils import (
    append_image_from_bytes,
    extract_images_from_markdown,
    is_image_stream,
    normalize_markdown,
    stream_info_from_url,
)


class ParserServiceError(Exception):
    pass


class ParserService:
    """
    文档解析服务

    输出策略（根据配置自动决定）:
    - 有模型 → 返回模型处理后的 markdown，无 images
    - 有 OSS + 无模型 → 返回 markdown + images (含 url)
    - 无 OSS + 无模型 → 返回 markdown + images (含 base64)
    """

    def __init__(self) -> None:
        self._md = MarkItDown()
        self._model_service = ModelService()

    def _has_model(self) -> bool:
        """检查是否配置了模型"""
        return bool(get_settings().model.api_key)

    def _clear_images_base64(self, images: list[dict]) -> None:
        """清理图片的 base64 数据"""
        for img in images:
            if "base64" in img:
                del img["base64"]

    @observe(name="parse_url")
    def parse_url(self, *, url: str) -> dict:
        """
        解析 URL 并返回处理后的文档

        Args:
            url: 要解析的 URL

        Returns:
            处理后的数据字典
        """
        overall_start = time.monotonic()

        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            try:
                logger.info("parse_url: START url={}", url)

                # 1. 下载文件
                download_start = time.monotonic()
                with fetch(url) as response:
                    response.raise_for_status()
                    stream_info = stream_info_from_url(url, response.headers)
                    logger.debug(
                        "parse_url: stream_info filename={} extension={} mimetype={}",
                        stream_info.filename,
                        stream_info.extension,
                        stream_info.mimetype,
                    )

                    shutil.copyfileobj(response.raw, tmp_file)
                    tmp_file.flush()
                    tmp_file.seek(0)

                logger.info(
                    "parse_url: download done elapsed_ms={}",
                    int((time.monotonic() - download_start) * 1000),
                )

                # 2. 转换文档
                convert_start = time.monotonic()
                with open(tmp_file.name, "rb") as f_read:
                    result = self._md.convert_stream(
                        f_read,
                        stream_info=stream_info,
                        keep_data_uris=True,  # 总是保留 data URI 以便提取
                    )
                markdown = normalize_markdown(result.text_content or "")
                del result  # 释放 MarkItDown 结果对象

                logger.info(
                    "parse_url: convert done elapsed_ms={}",
                    int((time.monotonic() - convert_start) * 1000),
                )

                # 3. 提取图片（总是执行）
                extract_start = time.monotonic()
                markdown, images = extract_images_from_markdown(markdown)

                # Fallback: 输入本身是图片但未能提取
                if not images and is_image_stream(stream_info):
                    with open(tmp_file.name, "rb") as f_read:
                        image_bytes = f_read.read()
                    markdown, images = append_image_from_bytes(
                        markdown, image_bytes, stream_info
                    )
                    logger.debug("parse_url: fallback image used, images={}", len(images))

                logger.info(
                    "parse_url: extract done images={} elapsed_ms={}",
                    len(images),
                    int((time.monotonic() - extract_start) * 1000),
                )

                # 4. 上传到 OSS（如果启用）
                if images and storage_enabled():
                    upload_start = time.monotonic()
                    try:
                        upload_images_concurrently(images)
                        # 注意：upload_images_concurrently 内部会在成功时 pop base64
                        # 失败的图片会保留 base64 作为 fallback
                        logger.info(
                            "parse_url: upload done images={} elapsed_ms={}",
                            len(images),
                            int((time.monotonic() - upload_start) * 1000),
                        )
                    except Exception as e:
                        logger.error("parse_url: storage upload failed: {}", e)
                        # 继续处理，不中断流程

                # 5. 模型处理
                # 如果没有图片，直接跳过模型处理（节省成本/时间），除非需要模型做纯文本清洗
                # 根据需求：没图片就直接返回结果
                if not images:
                    logger.info("parse_url: no images found, skipping model processing")
                    data = {"markdown": markdown}
                elif self._has_model():
                    model_start = time.monotonic()
                    try:
                        model_result = self._model_service.process_document_chunked(
                            markdown, images
                        )
                        if model_result:
                            markdown = model_result
                            logger.info(
                                "parse_url: model done length={} elapsed_ms={}",
                                len(model_result),
                                int((time.monotonic() - model_start) * 1000),
                            )
                    except Exception as e:
                        logger.error("parse_url: model processing failed: {}", e)
                        # 模型失败时回退到原始 markdown + images
                        self._build_fallback_response(markdown, images)
                    finally:
                        # 模型处理完成后清理所有图片的 base64
                        self._clear_images_base64(images)

                    # 有模型时，只返回 markdown
                    data = {"markdown": markdown}
                else:
                    # 无模型时，返回 markdown + images
                    data = {"markdown": markdown, "images": images}

                overall_elapsed = time.monotonic() - overall_start
                logger.info(
                    "parse_url: COMPLETE elapsed_ms={} chars={} images={}",
                    int(overall_elapsed * 1000),
                    len(markdown),
                    len(images),
                )

                gc.collect()
                return data

            except requests.RequestException as exc:
                logger.exception("parse_url: download failed")
                raise ParserServiceError(f"download failed: {exc}") from exc
            except (UnsupportedFormatException, FileConversionException, MarkItDownException) as exc:
                logger.exception("parse_url: conversion failed")
                raise ParserServiceError(f"conversion failed: {exc}") from exc
            except Exception as exc:
                logger.exception("parse_url: unknown error")
                raise ParserServiceError(f"unknown error: {exc}") from exc
            finally:
                gc.collect()

    @observe(name="parse_file")
    def parse_file(self, *, file) -> dict:
        """
        解析上传的文件并返回处理后的文档

        Args:
            file: 上传的文件对象

        Returns:
            处理后的数据字典
        """
        overall_start = time.monotonic()

        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            try:
                logger.info(
                    "parse_file: START filename={} content_type={}",
                    file.filename,
                    file.content_type,
                )

                # 1. 复制文件到临时目录
                file.file.seek(0)
                shutil.copyfileobj(file.file, tmp_file)
                tmp_file.flush()
                tmp_file.seek(0)

                # 2. 转换文档
                convert_start = time.monotonic()
                stream_info = StreamInfo(
                    filename=file.filename,
                    extension=os.path.splitext(file.filename)[1] or None,
                    mimetype=file.content_type,
                )

                with open(tmp_file.name, "rb") as f_read:
                    result = self._md.convert_stream(
                        f_read,
                        stream_info=stream_info,
                        keep_data_uris=True,
                    )
                markdown = normalize_markdown(result.text_content or "")
                del result

                logger.info(
                    "parse_file: convert done elapsed_ms={}",
                    int((time.monotonic() - convert_start) * 1000),
                )

                # 3. 提取图片
                extract_start = time.monotonic()
                markdown, images = extract_images_from_markdown(markdown)

                if not images and is_image_stream(stream_info):
                    with open(tmp_file.name, "rb") as f_read:
                        image_bytes = f_read.read()
                    markdown, images = append_image_from_bytes(
                        markdown, image_bytes, stream_info
                    )
                    logger.debug("parse_file: fallback image used, images={}", len(images))

                logger.info(
                    "parse_file: extract done images={} elapsed_ms={}",
                    len(images),
                    int((time.monotonic() - extract_start) * 1000),
                )

                # 4. 上传到 OSS
                if images and storage_enabled():
                    upload_start = time.monotonic()
                    try:
                        upload_images_concurrently(images)
                        logger.info(
                            "parse_file: upload done images={} elapsed_ms={}",
                            len(images),
                            int((time.monotonic() - upload_start) * 1000),
                        )
                    except Exception as e:
                        logger.error("parse_file: storage upload failed: {}", e)

                # 5. 模型处理
                if not images:
                    logger.info("parse_file: no images found, skipping model processing")
                    data = {"markdown": markdown}
                elif self._has_model():
                    model_start = time.monotonic()
                    try:
                        model_result = self._model_service.process_document_chunked(
                            markdown, images
                        )
                        if model_result:
                            markdown = model_result
                            logger.info(
                                "parse_file: model done length={} elapsed_ms={}",
                                len(model_result),
                                int((time.monotonic() - model_start) * 1000),
                            )
                    except Exception as e:
                        logger.error("parse_file: model processing failed: {}", e)
                    finally:
                        self._clear_images_base64(images)

                    data = {"markdown": markdown}
                else:
                    data = {"markdown": markdown, "images": images}

                overall_elapsed = time.monotonic() - overall_start
                logger.info(
                    "parse_file: COMPLETE elapsed_ms={} chars={} images={}",
                    int(overall_elapsed * 1000),
                    len(markdown),
                    len(images),
                )

                gc.collect()
                return data

            except (UnsupportedFormatException, FileConversionException, MarkItDownException) as exc:
                logger.exception("parse_file: conversion failed")
                raise ParserServiceError(f"conversion failed: {exc}") from exc
            except Exception as exc:
                logger.exception("parse_file: unknown error")
                raise ParserServiceError(f"unknown error: {exc}") from exc
            finally:
                gc.collect()

    def _build_fallback_response(self, markdown: str, images: list[dict]) -> dict:
        """构建回退响应（模型失败时使用）"""
        return {"markdown": markdown, "images": images}
