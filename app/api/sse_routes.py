"""SSE streaming routes for real-time progress updates."""

import asyncio
import gc
import os
import shutil
import tempfile
import time

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from markitdown import MarkItDown, StreamInfo
from pydantic import BaseModel

from app.config import get_settings
from app.infra.downloader import fetch
from app.infra.storage_client import storage_enabled, upload_images_concurrently
from app.services.model_service import ModelService
from app.utils.parse_utils import (
    extract_images_from_markdown,
    is_image_stream,
    append_image_from_bytes,
    normalize_markdown,
    stream_info_from_url,
)
from app.utils.sse_events import SSEEventType
from app.utils.sse_generator import SSEEventGenerator
from app.utils.trace import get_trace_id, generate_trace_id

router = APIRouter()


class UrlStreamRequest(BaseModel):
    url: str


@router.post("/convert/file/stream")
async def convert_file_stream(file: UploadFile = File(...)) -> StreamingResponse:
    """SSE 流式文件转换接口"""
    trace_id = get_trace_id() or generate_trace_id()

    async def event_generator():
        gen = SSEEventGenerator(trace_id)
        event_queue = asyncio.Queue()
        gen.set_queue(event_queue)

        process_task = asyncio.create_task(
            _process_file_task(gen, file, event_queue)
        )
        async for event in _event_stream_wrapper(gen, event_queue, process_task):
            yield event

        # 确保文件关闭
        try:
            file.file.close()
        except Exception:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_sse_headers(trace_id)
    )


@router.post("/convert/url/stream")
async def convert_url_stream(payload: UrlStreamRequest) -> StreamingResponse:
    """SSE 流式 URL 转换接口"""
    trace_id = get_trace_id() or generate_trace_id()

    async def event_generator():
        gen = SSEEventGenerator(trace_id)
        event_queue = asyncio.Queue()
        gen.set_queue(event_queue)

        process_task = asyncio.create_task(
            _process_url_task(gen, payload.url, event_queue)
        )
        async for event in _event_stream_wrapper(gen, event_queue, process_task):
            yield event

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_sse_headers(trace_id)
    )


def _sse_headers(trace_id: str) -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-Trace-Id": trace_id,
    }


async def _event_stream_wrapper(gen, event_queue, process_task):
    """共同的 SSE 事件循环处理"""
    try:
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                yield event
                if '"type": "complete"' in event or '"type": "error"' in event:
                    break
            except asyncio.TimeoutError:
                if process_task.done():
                    break
                continue
    except asyncio.CancelledError:
        logger.warning("SSE stream cancelled: trace_id={}", gen.trace_id)
        process_task.cancel()
        raise
    except Exception as e:
        logger.exception("SSE stream error: trace_id={}", gen.trace_id)
        yield gen.create_event(SSEEventType.ERROR, str(e))


async def _process_file_task(gen: SSEEventGenerator, file: UploadFile, event_queue: asyncio.Queue):
    """处理上传文件"""
    try:
        await event_queue.put(gen.create_event(SSEEventType.STARTED, "开始处理文档", 0))
        logger.info("SSE processing file START: filename={} trace_id={}", file.filename, gen.trace_id)

        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            # Copy file
            async with gen.stage("uploading_local", "读取文件", 0, 5, enable_heartbeat=True):
                file.file.seek(0)
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: shutil.copyfileobj(file.file, tmp_file)
                )
                tmp_file.flush()
                tmp_file.seek(0)

            await _execute_streaming_pipeline(
                gen, event_queue,
                file_path=tmp_file.name,
                filename=file.filename,
                content_type=file.content_type,
                base_progress=5
            )

    except Exception as e:
        logger.exception("SSE processing file ERROR")
        await event_queue.put(gen.create_event(SSEEventType.ERROR, str(e)))


async def _process_url_task(gen: SSEEventGenerator, url: str, event_queue: asyncio.Queue):
    """处理 URL 下载"""
    try:
        await event_queue.put(gen.create_event(SSEEventType.STARTED, "开始处理 URL", 0))
        logger.info("SSE processing url START: url={} trace_id={}", url, gen.trace_id)

        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            # Download
            stream_info = None
            async with gen.stage("downloading", "下载文件", 0, 10, enable_heartbeat=True):
                def _download():
                    with fetch(url) as response:
                        response.raise_for_status()
                        shutil.copyfileobj(response.raw, tmp_file)
                        tmp_file.flush()
                        return stream_info_from_url(url, response.headers)

                stream_info = await asyncio.get_event_loop().run_in_executor(None, _download)
                if not stream_info.filename:
                    stream_info = StreamInfo(
                        url=stream_info.url,
                        filename="downloaded_content",
                        extension=stream_info.extension,
                        mimetype=stream_info.mimetype,
                        charset=getattr(stream_info, "charset", None)
                    )
                tmp_file.seek(0)

            await _execute_streaming_pipeline(
                gen, event_queue,
                file_path=tmp_file.name,
                filename=stream_info.filename,
                content_type=stream_info.mimetype,
                base_progress=10
            )

    except Exception as e:
        logger.exception("SSE processing url ERROR")
        await event_queue.put(gen.create_event(SSEEventType.ERROR, str(e)))


async def _execute_streaming_pipeline(
    gen: SSEEventGenerator,
    event_queue: asyncio.Queue,
    file_path: str,
    filename: str,
    content_type: str,
    base_progress: int
):
    """核心处理流水线"""
    settings = get_settings().model
    has_model = bool(settings.api_key)

    # 计算进度区间
    # Convert: base -> base+5
    # Extract: base+5 -> base+15
    # Upload: base+15 -> base+25
    # Model: base+25 -> 95
    p1 = base_progress
    p2 = p1 + 5
    p3 = p2 + 10
    p4 = p3 + 10

    # 阶段 1: 文档转换
    async with gen.stage("converting", "文档转换中", p1, p2, enable_heartbeat=True):
        stream_info = StreamInfo(
            filename=filename,
            extension=os.path.splitext(filename)[1] or None,
            mimetype=content_type,
        )

        md = MarkItDown()
        with open(file_path, "rb") as f_read:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: md.convert_stream(f_read, stream_info=stream_info, keep_data_uris=True)
            )

        markdown = normalize_markdown(result.text_content or "")
        del result
        del md

    # 阶段 2: 图片提取
    images = []
    async with gen.stage("extracting", "提取图片", p2, p3, enable_heartbeat=True):
        markdown, images = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: extract_images_from_markdown(markdown)
        )

        if not images and is_image_stream(stream_info):
            with open(file_path, "rb") as f_read:
                image_bytes = f_read.read()
            markdown, images = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: append_image_from_bytes(markdown, image_bytes, stream_info)
            )

    # 阶段 3: 对象存储上传
    if images and storage_enabled():
        async with gen.stage("uploading", f"上传图片 ({len(images)}张)", p3, p4, enable_heartbeat=True):
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: upload_images_concurrently(images)
            )

    # 阶段 4: 模型处理
    if has_model and not images:
        logger.info("SSE processing: no images found, skipping model processing")

    if has_model and images:
        async with gen.stage("model_processing", "AI 分析中", p4, 95, enable_heartbeat=True):
            model_service = ModelService()

            content = model_service._build_content(
                markdown, images, settings.task_prompt
            )
            messages = []
            if settings.system_prompt:
                messages.append({"role": "system", "content": settings.system_prompt})
            messages.append({"role": "user", "content": content})

            model_start_time = time.monotonic()
            ttfb = None
            full_result = ""
            chunk_count = 0

            def process_stream():
                chunks = []
                for chunk in model_service._call_model_stream(messages):
                    chunks.append(chunk)
                return chunks

            loop = asyncio.get_event_loop()
            chunks = await loop.run_in_executor(None, process_stream)

            for chunk in chunks:
                if ttfb is None and chunk:
                    ttfb = time.monotonic() - model_start_time

                chunk_count += 1
                await event_queue.put(
                    gen.create_event(SSEEventType.MODEL_CHUNK, "", data={"content": chunk})
                )
                full_result += chunk

            total_time = time.monotonic() - model_start_time
            ttfb_ms = int(ttfb * 1000) if ttfb else 0
            total_ms = int(total_time * 1000)
            output_chars = len(full_result)
            chars_per_sec = output_chars / total_time if total_time > 0 else 0
            avg_chunk_size = output_chars / chunk_count if chunk_count > 0 else 0

            logger.info(
                "Model performance: model={} ttfb_ms={} total_ms={} output_chars={} "
                "chars_per_sec={:.2f} chunks={} avg_chunk_size={:.1f} trace_id={}",
                settings.model_name,
                ttfb_ms,
                total_ms,
                output_chars,
                chars_per_sec,
                chunk_count,
                avg_chunk_size,
                gen.trace_id
            )

            if full_result:
                markdown = full_result

    # 如果没有进行模型流式处理（无模型或无图片），则模拟发送一个 chunk
    # 这样客户端可以统一只监听 model_chunk 获取内容
    if not (has_model and images):
        chunk_data = {"content": markdown}
        if images:
            chunk_data["images"] = images

        await event_queue.put(
            gen.create_event(SSEEventType.MODEL_CHUNK, "", data=chunk_data)
        )

    # GC: Clear base64
    for img in images:
        if "base64" in img:
            del img["base64"]

    # 完成事件（纯净，仅表示状态）
    await event_queue.put(
        gen.create_event(SSEEventType.COMPLETE, "处理完成", 100)
    )
    logger.info("SSE pipeline COMPLETE: filename={} trace_id={}", filename, gen.trace_id)

    # 最终 GC
    del markdown
    del images
    gc.collect()
    logger.debug("GC completed: trace_id={}", gen.trace_id)
