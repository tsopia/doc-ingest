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
from app.utils.denoise import clean_markdown_v1, should_ai_denoise
from app.utils.observability import observe
from app.utils.parse_utils import (
    append_image_from_bytes,
    extract_images_from_markdown,
    is_image_stream,
    normalize_markdown,
    stream_info_from_url,
)
from app.utils.profiler import RequestProfiler

# 有效 mode 值
VALID_MODES = {"ocr", "semantic"}


class ParserServiceError(Exception):
    pass


class ParserService:
    """
    文档解析服务

    支持两种处理模式:
    - ocr: MarkItDown + OCR 插件直接提取图片文字 → 代码去噪 → 干净 markdown
    - semantic: 现有链路，MarkItDown → 提取图片 → 上传 OSS → 模型描述图片

    无模型配置时: 忽略 mode，走纯 MarkItDown 转换
    """

    def __init__(self) -> None:
        self._md = MarkItDown()
        self._model_service = ModelService()

        # OCR 实例（仅在有模型配置时初始化）
        self._md_ocr: MarkItDown | None = None
        if self._has_model():
            self._init_ocr_instance()

    def _has_model(self) -> bool:
        """检查是否配置了模型"""
        return bool(get_settings().model.api_key)

    def _init_ocr_instance(self) -> None:
        """初始化带 OCR 插件的 MarkItDown 实例"""
        from app.utils.observability import get_openai_client

        settings = get_settings().model
        try:
            client = get_openai_client(
                api_key=settings.api_key,
                base_url=settings.base_url or None,
            )
            self._md_ocr = MarkItDown(
                enable_plugins=True,
                llm_client=client,
                llm_model=settings.model_name,
                llm_prompt=settings.ocr_prompt,
            )
            logger.info("OCR MarkItDown instance initialized: model={}", settings.model_name)
        except Exception as e:
            logger.error("Failed to initialize OCR MarkItDown instance: {}", e)
            self._md_ocr = None

    def _resolve_mode(self, mode: str) -> str:
        """解析有效的处理模式，处理降级逻辑。

        Returns:
            实际生效的 mode: "ocr" / "semantic" / "none"(无模型)
        """
        if not self._has_model():
            if mode in VALID_MODES:
                logger.warning(
                    "Requested mode='{}' but no model configured, falling back to plain conversion",
                    mode,
                )
            return "none"

        if mode not in VALID_MODES:
            logger.warning("Invalid mode='{}', defaulting to 'ocr'", mode)
            return "ocr"

        if mode == "ocr" and self._md_ocr is None:
            logger.warning("OCR instance not available, falling back to 'semantic'")
            return "semantic"

        return mode

    def _should_run_ai_denoise(self, enable: str, stats: dict, markdown: str) -> tuple[bool, str]:
        """判断是否应该执行 AI 去噪。
        
        Args:
            enable: "auto", "true", "false"
            stats: clean_markdown_v1 返回的统计信息
            markdown: 当前处理后的文本
            
        Returns:
            (是否执行, 触发原因)
        """
        enable = enable.lower()
        if enable == "false":
            return False, "disabled by user"
        if enable == "true":
            return True, "forced by user"
            
        # 默认 auto
        return should_ai_denoise(stats, markdown)

    @observe()
    async def process_workflow(
        self,
        source: str | object,  # URL str or UploadFile object
        source_type: str = "url",  # "url" or "file"
        mode: str = "ocr",
        enable_ai_denoise: str = "auto",
        enable_streaming: bool = False,
        accumulate_model_output: bool = True,
    ):
        """
        统一的文档处理工作流（Async Generator）

        Args:
            source: URL 字符串 或 UploadFile 对象
            source_type: "url" 或 "file"
            mode: 处理模式 "ocr" 或 "semantic"
            enable_streaming: 是否启用模型流式输出
            accumulate_model_output: 是否在服务端拼接模型输出（仅在 enable_streaming=True 时有效）

        Yields:
            dict: 事件字典，包含 type, stage, message, progress, data, content 等字段
        """
        import asyncio
        import time

        from app.utils.trace import get_trace_id

        effective_mode = self._resolve_mode(mode)

        # 进度区间定义
        P_START = 0
        P_DOWNLOAD = 10
        P_CONVERT = 15
        P_EXTRACT = 25
        P_UPLOAD = 35
        P_MODEL_START = 35
        P_MODEL_END = 95
        P_DENOISE = 95  # OCR 模式去噪阶段
        P_DONE = 100

        # 上下文数据
        markdown = ""
        images = []
        filename = "unknown"

        tmp_file_path = None

        # 性能 profiler
        profiler = RequestProfiler(trace_id=get_trace_id() or "")
        profiler.start()

        try:
            # === Stage 1: 准备/下载 ===
            profiler.stage_begin("preparation")
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
                profiler.stage_end("preparation")
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
                profiler.stage_end("preparation")
                yield {"type": "stage_end", "stage": "preparation", "progress": P_DOWNLOAD}

            if not stream_info:
                 raise ParserServiceError("Failed to determine stream info")

            # === Stage 2: 转换 ===
            profiler.stage_begin("converting")
            yield {"type": "stage_start", "stage": "converting", "message": "文档转换中", "progress": P_DOWNLOAD}

            # 根据模式选择 MarkItDown 实例
            md_instance = self._md_ocr if effective_mode == "ocr" else self._md

            def _convert():
                with open(tmp_file_path, "rb") as f_read:
                    result = md_instance.convert_stream(
                        f_read,
                        stream_info=stream_info,
                        keep_data_uris=True
                    )
                raw_text = result.text_content or ""
                # 显式清理 result 以尽早释放 MarkItDown 内部数据
                del result
                
                if effective_mode == "ocr":
                    # OCR 模式没有大段 base64 图像，直接 normalize
                    return normalize_markdown(raw_text)
                else:
                    # semantic 模式文本包含大量 base64 数据，
                    # 如果此时执行 normalize_markdown 会因字符串复制引发巨大的内存尖峰 (OOM 隐患)。
                    # 因此原样返回，留到 extract 阶段剔除 base64 后再做 normalize。
                    return raw_text

            markdown = await asyncio.to_thread(_convert)
            raw_length = len(markdown)
            profiler.stage_end("converting", markdown_length=raw_length)
            yield {"type": "stage_end", "stage": "converting", "progress": P_CONVERT}

            # === OCR 模式：转换后直接去噪，跳过图片提取/上传/模型处理 ===
            if effective_mode == "ocr":
                profiler.stage_begin("denoising")
                yield {"type": "stage_start", "stage": "denoising", "message": "去噪清洗中", "progress": P_CONVERT}

                clean_result = await asyncio.to_thread(clean_markdown_v1, markdown)
                final_markdown = clean_result.markdown

                profiler.stage_end("denoising", clean_length=len(final_markdown))
                yield {"type": "stage_end", "stage": "denoising", "progress": P_DENOISE}

                meta = {
                    "mode": "ocr",
                    "raw_length": raw_length,
                    "clean_length": len(final_markdown),
                    **clean_result.stats,
                }

                # === Stage: AI 去噪（可选） ===
                ai_denoise_applied = False
                ai_denoise_trigger = ""
                
                should_run, trigger_reason = self._should_run_ai_denoise(
                    enable_ai_denoise, clean_result.stats, final_markdown
                )
                
                if should_run:
                    logger.info(f"Starting AI denoising, reason: {trigger_reason}")
                    profiler.stage_begin("ai_denoising")
                    yield {"type": "stage_start", "stage": "ai_denoising", "message": "AI 深度去噪中", "progress": P_DENOISE}
                    
                    try:
                        ai_result = await asyncio.to_thread(self._model_service.denoise_text, final_markdown)
                        if ai_result:
                            ai_denoise_applied = True
                            ai_denoise_trigger = trigger_reason
                            meta["ai_denoise_applied"] = True
                            meta["ai_denoise_trigger"] = ai_denoise_trigger
                            meta["ai_denoise_length_before"] = len(final_markdown)
                            final_markdown = ai_result
                            meta["ai_denoise_length_after"] = len(final_markdown)
                            meta["clean_length"] = len(final_markdown)
                    except Exception as e:
                        logger.warning(f"AI denoising failed, falling back to v1 result: {e}")
                        meta["ai_denoise_applied"] = False
                        meta["ai_denoise_error"] = str(e)
                        
                    profiler.stage_end("ai_denoising", final_length=len(final_markdown))
                    yield {"type": "stage_end", "stage": "ai_denoising", "progress": P_DENOISE}
                else:
                    meta["ai_denoise_applied"] = False
                    meta["ai_denoise_trigger"] = trigger_reason

                result_data = {
                    "markdown": final_markdown,
                    "meta": meta,
                }

                yield {"type": "result", "data": result_data, "progress": P_DONE}
                return  # OCR 链路结束

            # === semantic 模式 / none 模式：走原有链路 ===

            # === Stage 3: 提取图片 ===
            profiler.stage_begin("extracting")
            yield {"type": "stage_start", "stage": "extracting", "message": "提取图片", "progress": P_CONVERT}

            def _extract():
                md, imgs = extract_images_from_markdown(markdown)
                # 在抽离了占空间的 base64 后再执行 normalize，极大降低内存占用峰值
                md = normalize_markdown(md)
                
                # Fallback check
                if not imgs and is_image_stream(stream_info):
                    with open(tmp_file_path, "rb") as f_read:
                        img_bytes = f_read.read()
                    md, imgs = append_image_from_bytes(md, img_bytes, stream_info)
                return md, imgs

            markdown, images = await asyncio.to_thread(_extract)
            # semantic 模式：_extract 内部已将 base64 大文本拆解为占位符，
            # 旧的 raw_text 引用可能还被 generator frame 持有，主动触发一次回收降低内存尖峰
            gc.collect()
            profiler.stage_end("extracting", image_count=len(images))
            yield {"type": "stage_end", "stage": "extracting", "data": {"image_count": len(images)}, "progress": P_EXTRACT}

            # === Stage 4: 上传图片 ===
            if images and storage_enabled():
                profiler.stage_begin("uploading")
                yield {"type": "stage_start", "stage": "uploading", "message": f"上传图片 ({len(images)}张)", "progress": P_EXTRACT}
                # storage upload is IO bound but implemented with threads in storage_client, acceptable to run in executor
                await asyncio.to_thread(upload_images_concurrently, images)
                profiler.stage_end("uploading", image_count=len(images))
                yield {"type": "stage_end", "stage": "uploading", "progress": P_UPLOAD}

            # === Stage 5: 模型处理 ===
            has_model = self._has_model()
            final_markdown = markdown

            if has_model and images:
                profiler.stage_begin("model_processing")
                yield {"type": "stage_start", "stage": "model_processing", "message": "AI 分析中", "progress": P_MODEL_START}

                if enable_streaming:
                    # 流式处理
                    from starlette.concurrency import iterate_in_threadpool

                    # 用 list + join 避免字符串 += 的 O(n²) 内存分配
                    content_parts: list[str] = []
                    async for chunk in iterate_in_threadpool(self._model_service.process_document_stream(markdown, images)):
                        if accumulate_model_output:
                            content_parts.append(chunk)
                        yield {"type": "model_chunk", "content": chunk}

                    if accumulate_model_output:
                        final_markdown = "".join(content_parts)
                        del content_parts
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

                profiler.stage_end("model_processing", result_length=len(final_markdown))
                yield {"type": "stage_end", "stage": "model_processing", "progress": P_MODEL_END}

            elif not images:
                # No images log
                logger.info("process_workflow: no images found, skipping model")

            # === Result ===
            result_data = {
                "markdown": final_markdown,
                "meta": {
                    "mode": effective_mode if effective_mode != "none" else "plain",
                },
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
            # 不再 re-raise：eraise 会导致 SSE 调用方的 except 再次发送一个 error 事件，客户端收到双重 error
            # 同步接口通过 parse_url/parse_file 内部的异步生成器迭代捕获 error 事件并转换为异常
            return
        finally:
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    os.unlink(tmp_file_path)
                except OSError:
                    pass
            profiler.stage_begin("gc")
            gc.collect()
            profiler.stage_end("gc")
            profiler.finish()

    def parse_url(self, *, url: str, mode: str = "ocr", enable_ai_denoise: str = "auto") -> dict:
        """
        同步兼容接口：解析 URL
        """
        import asyncio

        async def _run():
            result = None
            async for event in self.process_workflow(
                url, "url", mode=mode, enable_ai_denoise=enable_ai_denoise, enable_streaming=False
            ):
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

    def parse_file(self, *, file, mode: str = "ocr", enable_ai_denoise: str = "auto") -> dict:
        """
        同步兼容接口：解析文件
        """
        import asyncio

        async def _run():
            result = None
            async for event in self.process_workflow(
                file, "file", mode=mode, enable_ai_denoise=enable_ai_denoise, enable_streaming=False
            ):
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
