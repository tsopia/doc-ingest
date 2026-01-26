"""SSE streaming routes for real-time progress updates."""

import asyncio
import gc

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from app.services.parser_service import ParserService
from app.utils.sse_events import SSEEventType
from app.utils.sse_generator import SSEEventGenerator
from app.utils.trace import get_trace_id, generate_trace_id

router = APIRouter()
_service = ParserService()


class UrlStreamRequest(BaseModel):
    url: str


@router.post("/convert/file/stream")
async def convert_file_stream(file: UploadFile = File(...)) -> StreamingResponse:
    """SSE 流式文件转换接口"""
    trace_id = get_trace_id() or generate_trace_id()

    async def event_generator():
        gen = SSEEventGenerator(trace_id)
        # SSE Loop logic here

        try:
            # 发送初始事件
            yield gen.create_event(SSEEventType.STARTED, "开始处理文档", 0)
            logger.info("SSE processing file START: filename={} trace_id={}", file.filename, trace_id)

            async for event in _service.process_workflow(
                file, "file", enable_streaming=True
            ):
                yield _map_event(gen, event)

            yield gen.create_event(SSEEventType.COMPLETE, "处理完成", 100)
            logger.info("SSE pipeline COMPLETE: filename={} trace_id={}", file.filename, trace_id)

        except Exception as e:
            logger.exception("SSE processing file ERROR")
            yield gen.create_event(SSEEventType.ERROR, str(e))
        finally:
            file.file.close()

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

        try:
            yield gen.create_event(SSEEventType.STARTED, "开始处理 URL", 0)
            logger.info("SSE processing url START: url={} trace_id={}", payload.url, trace_id)

            async for event in _service.process_workflow(
                payload.url, "url", enable_streaming=True
            ):
                yield _map_event(gen, event)

            yield gen.create_event(SSEEventType.COMPLETE, "处理完成", 100)
            logger.info("SSE pipeline COMPLETE: url={} trace_id={}", payload.url, trace_id)

        except Exception as e:
            logger.exception("SSE processing url ERROR")
            yield gen.create_event(SSEEventType.ERROR, str(e))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_sse_headers(trace_id)
    )


def _map_event(gen: SSEEventGenerator, event: dict) -> str:
    """Map ParserService events to SSE format"""
    etype = event.get("type")

    if etype == "stage_start":
        return gen.create_event(
            f"stage:{event.get('stage')}",
            event.get("message", ""),
            event.get("progress")
        )
    elif etype == "stage_end":
        msg = f"{event.get('message', '')} 完成" if event.get('message') else "完成"
        # stage_end event usually carries data
        return gen.create_event(
            f"stage:{event.get('stage')}_done",
            msg,
            event.get("progress"),
            data=event.get("data")
        )
    elif etype == "progress":
        return gen.create_event(
            "progress", # Or use HEARTBEAT often? No, explicit progress
            event.get("message", ""),
            event.get("progress")
        )
    elif etype == "model_chunk":
        # Standardize model chunk output
        return gen.create_event(
            SSEEventType.MODEL_CHUNK,
            "",
            data={"content": event.get("content")}
        )
    elif etype == "result":
        # Final result data (markdown/images)
        # Usually sent as model_chunk or separate result?
        # SSE clients expect model_chunk for content.
        # If model stream happened, we already sent content.
        # If no model stream (image only or sync), we might need to send result here.

        # Check if result contains markdown and we haven't streamed it
        # Actually logic in ParserService:
        # If enabled_streaming=True, it streams chunks.
        # But 'result' event contains full data.
        # We can send a final chunk with images if needed?

        # Consistent with old sse logic:
        # "If not (has_model and images): await event_queue.put(chunk_data)"

        return gen.create_event(
            SSEEventType.MODEL_CHUNK,
            "",
            data=event.get("data")
        )
    elif etype == "error":
         return gen.create_event(SSEEventType.ERROR, event.get("message"))

    return gen.create_event("unknown", str(event))


def _sse_headers(trace_id: str) -> dict:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-Trace-Id": trace_id,
    }
