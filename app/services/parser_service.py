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

    @observe()
    async def process_workflow(
        self,
        source: str | object,  # URL str or UploadFile object
        source_type: str = "url",  # "url" or "file"
        enable_streaming: bool = False,
        accumulate_model_output: bool = True,
    ):
        """
        统一的文档处理工作流（Async Generator）

        Args:
            source: URL 字符串 或 UploadFile 对象
            source_type: "url" 或 "file"
            enable_streaming: 是否启用模型流式输出
            accumulate_model_output: 是否在服务端拼接模型输出（仅在 enable_streaming=True 时有效）

        Yields:
            dict: 事件字典，包含 type, stage, message, progress, data, content 等字段
        """
        import asyncio
        import time

        # 进度区间定义
        P_START = 0
        P_DOWNLOAD = 10
        P_CONVERT = 15
        P_EXTRACT = 25
        P_UPLOAD = 35
        P_MODEL_START = 35
        P_MODEL_END = 95
        P_DONE = 100

        # 上下文数据
        markdown = ""
        images = []
        filename = "unknown"

        tmp_file_path = None

        try:
            # === Stage 1: 准备/下载 ===
            yield {"type": "stage_start", "stage": "preparation", "message": "准备资源", "progress": P_START}

            # 使用临时文件
            fd, tmp_file_path = tempfile.mkstemp()
            os.close(fd)

            stream_info = None

            if source_type == "url":
                yield {"type": "progress", "message": "正在下载文档", "progress": P_START + 5}

                def _download():
                    with fetch(source) as response:
                        response.raise_for_status()
                        with open(tmp_file_path, "wb") as f:
                            shutil.copyfileobj(response.raw, f)
                        return stream_info_from_url(source, response.headers)

                stream_info = await asyncio.to_thread(_download)
                yield {"type": "stage_end", "stage": "preparation", "progress": P_DOWNLOAD}

            elif source_type == "file":
                yield {"type": "progress", "message": "正在读取文件", "progress": P_START + 5}
                # source is UploadFile
                filename = source.filename
                content_type = source.content_type

                def _save_upload():
                    source.file.seek(0)
                    with open(tmp_file_path, "wb") as f:
                        shutil.copyfileobj(source.file, f)
                    return StreamInfo(
                        filename=filename,
                        extension=os.path.splitext(filename)[1] or None,
                        mimetype=content_type
                    )

                stream_info = await asyncio.to_thread(_save_upload)
                yield {"type": "stage_end", "stage": "preparation", "progress": P_DOWNLOAD}

            if not stream_info:
                 raise ParserServiceError("Failed to determine stream info")

            # === Stage 2: 转换 ===
            yield {"type": "stage_start", "stage": "converting", "message": "文档转换中", "progress": P_DOWNLOAD}

            def _convert():
                with open(tmp_file_path, "rb") as f_read:
                    result = self._md.convert_stream(
                        f_read,
                        stream_info=stream_info,
                        keep_data_uris=True
                    )
                return normalize_markdown(result.text_content or "")

            markdown = await asyncio.to_thread(_convert)
            yield {"type": "stage_end", "stage": "converting", "progress": P_CONVERT}

            # === Stage 3: 提取图片 ===
            yield {"type": "stage_start", "stage": "extracting", "message": "提取图片", "progress": P_CONVERT}

            def _extract():
                md, imgs = extract_images_from_markdown(markdown)
                # Fallback check
                if not imgs and is_image_stream(stream_info):
                    with open(tmp_file_path, "rb") as f_read:
                        img_bytes = f_read.read()
                    md, imgs = append_image_from_bytes(md, img_bytes, stream_info)
                return md, imgs

            markdown, images = await asyncio.to_thread(_extract)
            yield {"type": "stage_end", "stage": "extracting", "data": {"image_count": len(images)}, "progress": P_EXTRACT}

            # === Stage 4: 上传图片 ===
            if images and storage_enabled():
                yield {"type": "stage_start", "stage": "uploading", "message": f"上传图片 ({len(images)}张)", "progress": P_EXTRACT}
                # storage upload is IO bound but implemented with threads in storage_client, acceptable to run in executor
                await asyncio.to_thread(upload_images_concurrently, images)
                yield {"type": "stage_end", "stage": "uploading", "progress": P_UPLOAD}

            # === Stage 5: 模型处理 ===
            # 如果无图片，也可能有纯文本处理需求？
            # 保持原有逻辑：无图片且有模型 -> 跳过，除非 forced (当前逻辑是跳过)
            # 有模型且有图片 -> 处理

            has_model = self._has_model()
            final_markdown = markdown

            if has_model and images:
                yield {"type": "stage_start", "stage": "model_processing", "message": "AI 分析中", "progress": P_MODEL_START}

                if enable_streaming:
                    # 流式处理
                    from starlette.concurrency import iterate_in_threadpool

                    full_content = ""
                    async for chunk in iterate_in_threadpool(self._model_service.process_document_stream(markdown, images)):
                         if accumulate_model_output:
                            full_content += chunk
                         yield {"type": "model_chunk", "content": chunk}

                    if accumulate_model_output:
                        final_markdown = full_content
                    else:
                        final_markdown = ""
                else:
                    # 阻塞处理
                    def _call_model():
                         return self._model_service.process_document_chunked(markdown, images)

                    result = await asyncio.to_thread(_call_model)
                    if result:
                        final_markdown = result

                # 清理 base64
                for img in images:
                    if "base64" in img:
                         del img["base64"]

                yield {"type": "stage_end", "stage": "model_processing", "progress": P_MODEL_END}

            elif not images:
                # No images log
                logger.info("process_workflow: no images found, skipping model")

            # === Result ===
            result_data = {
                "markdown": final_markdown,
                # 如果没有模型处理（或模型保留了图片），可能需要返回 images 列表给前端
                # 原逻辑：有模型 -> 只返回 markdown; 无模型 -> markdown + images
                # 这里我们返回所有信息，由 controller 决定怎么给
            }
            if not has_model or not images:
                 result_data["images"] = images

            # 仅当我们需要发送完整结果时才 yield
            # 如果是流式模式且禁用了累积，则不发送空的 result (model_chunk)
            if not (enable_streaming and has_model and images and not accumulate_model_output):
                yield {"type": "result", "data": result_data, "progress": P_DONE}

        except Exception as e:
            logger.exception("process_workflow failed")
            yield {"type": "error", "message": str(e)}
            raise e
        finally:
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    os.unlink(tmp_file_path)
                except OSError:
                    pass
            gc.collect()

    def parse_url(self, *, url: str) -> dict:
        """
        同步兼容接口：解析 URL
        """
        import asyncio

        async def _run():
            result = None
            async for event in self.process_workflow(url, "url", enable_streaming=False):
                if event["type"] == "result":
                    result = event["data"]
                elif event["type"] == "error":
                     raise ParserServiceError(event["message"])
            return result

        try:
            # 运行异步循环
            # 注意：如果已经在 loop 中（例如 fastAPI 线程池调用），这里创建新 loop 可能会有问题
            # 但 routes.py 是用 await asyncio.to_thread(_service.parse_url) 调用的
            # to_thread 会在独立线程运行，那里没有 loop。所以 asyncio.run 是安全的。
            return asyncio.run(_run())
        except Exception as e:
            if isinstance(e, ParserServiceError):
                raise e
            raise ParserServiceError(str(e))

    def parse_file(self, *, file) -> dict:
        """
        同步兼容接口：解析文件
        """
        import asyncio

        async def _run():
            result = None
            async for event in self.process_workflow(file, "file", enable_streaming=False):
                if event["type"] == "result":
                    result = event["data"]
                elif event["type"] == "error":
                     raise ParserServiceError(event["message"])
            return result

        try:
            return asyncio.run(_run())
        except Exception as e:
             if isinstance(e, ParserServiceError):
                raise e
             raise ParserServiceError(str(e))

    def _build_fallback_response(self, markdown: str, images: list[dict]) -> dict:
        """构建回退响应（模型失败时使用）"""
        return {"markdown": markdown, "images": images}
