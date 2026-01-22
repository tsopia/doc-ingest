"""Async wrapper for parser service to support SSE streaming."""

import asyncio
from typing import Optional, Sequence

from fastapi import UploadFile
from loguru import logger

from app.services.parser_service import ParserService


class AsyncParserService:
    """异步版本的 Parser Service，支持 SSE 流式响应"""

    def __init__(self):
        self._sync_service = ParserService()

    async def parse_file(
        self,
        *,
        file: UploadFile,
        structured: Optional[Sequence[str]],
        keep_data_uris: bool,
        extract_images: bool,
    ) -> dict:
        """
        异步解析文件（在线程池中运行同步代码）

        Args:
            file: 上传的文件
            structured: 结构化数据类型列表
            keep_data_uris: 是否保留 data URI
            extract_images: 是否提取图片

        Returns:
            解析结果字典
        """
        logger.debug(
            "async parse_file: filename={} structured={} keep_data_uris={} extract_images={}",
            file.filename,
            structured,
            keep_data_uris,
            extract_images
        )

        # 在线程池中运行同步代码
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._sync_service.parse_file(
                file=file,
                structured=structured,
                keep_data_uris=keep_data_uris,
                extract_images=extract_images,
            )
        )

        return result

    async def parse_url(
        self,
        *,
        url: str,
        structured: Optional[Sequence[str]],
        keep_data_uris: bool,
        extract_images: bool,
    ) -> dict:
        """
        异步解析 URL（在线程池中运行同步代码）

        Args:
            url: 文档 URL
            structured: 结构化数据类型列表
            keep_data_uris: 是否保留 data URI
            extract_images: 是否提取图片

        Returns:
            解析结果字典
        """
        logger.debug(
            "async parse_url: url={} structured={} keep_data_uris={} extract_images={}",
            url,
            structured,
            keep_data_uris,
            extract_images
        )

        # 在线程池中运行同步代码
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._sync_service.parse_url(
                url=url,
                structured=structured,
                keep_data_uris=keep_data_uris,
                extract_images=extract_images,
            )
        )

        return result
