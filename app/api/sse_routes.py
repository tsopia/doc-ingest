"""SSE streaming routes for real-time progress updates."""

import asyncio
import gc
import shutil
import tempfile
import time
from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from markitdown import MarkItDown, StreamInfo

from app.config import get_settings
from app.infra.storage_client import storage_enabled, upload_images_concurrently
from app.services.model_service import ModelService
from app.utils.parse_utils import (
    build_structured,
    extract_images_from_markdown,
    is_image_stream,
    append_image_from_bytes,
    normalize_markdown,
    parse_structured,
)
from app.utils.sse_events import SSEEventType
from app.utils.sse_generator import SSEEventGenerator
from app.utils.trace import get_trace_id, generate_trace_id
import os

router = APIRouter()


async def _async_wrap_generator(sync_gen):
    """
    将同步生成器包装为异步生成器

    Args:
        sync_gen: 同步生成器

    Yields:
        生成器产生的值
    """
    loop = asyncio.get_event_loop()

    while True:
        try:
            # 在线程池中获取下一个值
            value = await loop.run_in_executor(None, next, sync_gen, StopIteration)
            if value is StopIteration:
                break
            yield value
        except StopIteration:
            break


@router.post("/convert/file/stream")
async def convert_file_stream(
    file: UploadFile = File(...),
    structured: Optional[str] = Form(None),
    keep_data_uris: bool = Form(True),
    extract_images_param: bool = Form(True, alias="extract_images"),
) -> StreamingResponse:
    """
    SSE 流式文件转换接口

    实时推送处理进度，包括：
    - 文档转换
    - 图片提取
    - 对象存储上传
    - AI 模型处理
    - 结构化数据生成

    Args:
        file: 上传的文件
        structured: 结构化数据类型（逗号分隔）
        keep_data_uris: 是否保留 data URI
        extract_images_param: 是否提取图片

    Returns:
        SSE 流式响应
    """
    trace_id = get_trace_id() or generate_trace_id()

    async def event_generator():
        """SSE 事件生成器"""
        gen = SSEEventGenerator(trace_id)
        event_queue = asyncio.Queue()
        gen.set_queue(event_queue)

        # 创建后台任务处理文件
        process_task = asyncio.create_task(
            _process_file(
                gen,
                file,
                structured,
                keep_data_uris,
                extract_images_param,
                event_queue
            )
        )

        try:
            # 持续从队列中取事件并发送给客户端
            while True:
                try:
                    # 等待事件，超时时间设置为 1 秒
                    event = await asyncio.wait_for(event_queue.get(), timeout=1.0)
                    yield event

                    # 如果是完成或错误事件，结束流
                    if '"type": "complete"' in event or '"type": "error"' in event:
                        break

                except asyncio.TimeoutError:
                    # 超时继续等待，检查任务是否完成
                    if process_task.done():
                        break
                    continue

        except asyncio.CancelledError:
            logger.warning("SSE stream cancelled by client: trace_id={}", trace_id)
            process_task.cancel()
            raise
        except Exception as e:
            logger.exception("SSE stream error: trace_id={}", trace_id)
            yield gen.create_event(SSEEventType.ERROR, str(e))
        finally:
            # 确保文件被关闭
            try:
                file.file.close()
            except Exception as e:
                logger.debug("Error closing file: {}", e)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Trace-Id": trace_id,
        }
    )


async def _process_file(
    gen: SSEEventGenerator,
    file: UploadFile,
    structured: Optional[str],
    keep_data_uris: bool,
    extract_images_param: bool,
    event_queue: asyncio.Queue,
):
    """
    处理文件的后台任务

    Args:
        gen: SSE 事件生成器
        file: 上传的文件
        structured: 结构化数据类型
        keep_data_uris: 是否保留 data URI
        extract_images_param: 是否提取图片
        event_queue: 事件队列
    """
    try:
        # 发送开始事件
        await event_queue.put(
            gen.create_event(SSEEventType.STARTED, "开始处理文档", 0)
        )
        logger.info("SSE processing START: filename={} trace_id={}", file.filename, gen.trace_id)

        # 解析 structured 参数
        structured_list, err = parse_structured(structured)
        if err:
            await event_queue.put(gen.create_event(SSEEventType.ERROR, err))
            return

        # 规范化参数
        if extract_images_param and not keep_data_uris:
            keep_data_uris = True
            logger.warning("keep_data_uris overridden to True for extraction")

        settings = get_settings().model

        # 使用临时文件
        with tempfile.NamedTemporaryFile(delete=True) as tmp_file:
            # 阶段 1: 文档转换（启用探活）
            async with gen.stage("converting", "文档转换中", 5, 10, enable_heartbeat=True):
                # 复制文件到临时文件
                file.file.seek(0)
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: shutil.copyfileobj(file.file, tmp_file)
                )
                tmp_file.flush()
                tmp_file.seek(0)

                # 执行转换
                stream_info = StreamInfo(
                    filename=file.filename,
                    extension=os.path.splitext(file.filename)[1] or None,
                    mimetype=file.content_type,
                )

                md = MarkItDown()
                with open(tmp_file.name, "rb") as f_read:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: md.convert_stream(f_read, stream_info=stream_info, keep_data_uris=keep_data_uris)
                    )

                markdown = normalize_markdown(result.text_content or "")

                # 内存优化: 释放 MarkItDown 结果对象
                del result
                del md

            # 阶段 2: 图片提取（启用探活）
            images = []
            if extract_images_param:
                async with gen.stage("extracting", "提取图片", 10, 20, enable_heartbeat=True):
                    markdown, images = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: extract_images_from_markdown(markdown)
                    )

                    # Fallback: 如果没有提取到图片但输入是图片
                    if not images and is_image_stream(stream_info):
                        with open(tmp_file.name, "rb") as f_read:
                            image_bytes = f_read.read()
                        markdown, images = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: append_image_from_bytes(markdown, image_bytes, stream_info)
                        )

            # 阶段 3: 对象存储上传（启用探活）
            if images and storage_enabled():
                async with gen.stage("uploading", f"上传图片 ({len(images)}张)", 20, 30, enable_heartbeat=True):
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: upload_images_concurrently(images)
                    )

                # 内存优化: 上传完成后清理图片 base64 数据
                for img in images:
                    if "base64" in img:
                        del img["base64"]

            # 阶段 4: 模型处理（流式，禁用心跳，添加性能记录）
            if settings.api_key:
                async with gen.stage("model_processing", "AI 分析中", 30, 90, enable_heartbeat=True):
                    model_service = ModelService()

                    # 构建消息
                    content = model_service._build_content(
                        markdown, images, settings.task_prompt
                    )
                    messages = []
                    if settings.system_prompt:
                        messages.append({"role": "system", "content": settings.system_prompt})
                    messages.append({"role": "user", "content": content})

                    # 流式获取模型输出并逐 chunk 透传
                    # 性能指标
                    model_start_time = time.monotonic()
                    ttfb = None  # Time To First Byte
                    full_result = ""
                    chunk_count = 0

                    # 在同步上下文中执行流式调用
                    def process_stream():
                        """在线程中执行流式调用并收集结果"""
                        chunks = []
                        for chunk in model_service._call_model_stream(messages):
                            chunks.append(chunk)
                        return chunks

                    # 在线程池中执行
                    loop = asyncio.get_event_loop()
                    chunks = await loop.run_in_executor(None, process_stream)

                    # 逐 chunk 透传给客户端
                    for chunk in chunks:
                        # 记录首字时间
                        if ttfb is None and chunk:
                            ttfb = time.monotonic() - model_start_time

                        chunk_count += 1
                        # 透传给客户端
                        await event_queue.put(
                            gen.create_event(SSEEventType.MODEL_CHUNK, "", data={"content": chunk})
                        )
                        full_result += chunk

                    # 计算性能指标
                    total_time = time.monotonic() - model_start_time
                    ttfb_ms = int(ttfb * 1000) if ttfb else 0
                    total_ms = int(total_time * 1000)
                    output_chars = len(full_result)
                    chars_per_sec = output_chars / total_time if total_time > 0 else 0
                    avg_chunk_size = output_chars / chunk_count if chunk_count > 0 else 0

                    # 记录模型性能数据
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

                    # 内存优化: 释放模型处理中间变量
                    del chunks
                    del content
                    del messages
                    del full_result


            # 阶段 5: 结构化处理（传递structured数据）
            structured_data = None
            if structured_list:
                async with gen.stage("structuring", "结构化数据处理", 90, 95, enable_heartbeat=True):
                    structured_data = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda: build_structured(markdown, structured_list)
                    )

            # 发送完成事件（无 data，仅作为结束标记）
            await event_queue.put(
                gen.create_event(SSEEventType.COMPLETE, "处理完成", 100)
            )
            logger.info("SSE processing COMPLETE: filename={} trace_id={}", file.filename, gen.trace_id)

            # 内存优化: 请求完成后主动触发 GC
            del markdown
            del images
            if structured_data:
                del structured_data
            gc.collect()
            logger.debug("GC completed after SSE processing: trace_id={}", gen.trace_id)

    except Exception as e:
        logger.exception("SSE processing ERROR: filename={} trace_id={}", file.filename, gen.trace_id)
        await event_queue.put(
            gen.create_event(SSEEventType.ERROR, str(e), data={"error": str(e)})
        )
