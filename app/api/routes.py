import asyncio
import os
import shutil
import tempfile
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from pydantic import BaseModel

from app.services.parser_service import ParserService, ParserServiceError
from app.utils.callback import send_callback
from app.utils.trace import get_trace_id, generate_trace_id

router = APIRouter()
_service = ParserService()


@router.get("/health")
def health_check() -> dict:
    """健康检查端点,用于 K8s/Docker 等容器编排工具"""
    return {
        "status": "healthy",
        "service": "doc-ingest",
        "version": "0.1.0"
    }


class UrlRequest(BaseModel):
    url: str
    callback_url: Optional[str] = None


def _ok(data: dict) -> dict:
    return {"code": 0, "data": data, "msg": ""}


def _err(message: str, code: int = 1) -> dict:
    return {"code": code, "data": {}, "msg": message}


class DummyUploadFile:
    """Helper class to mimic UploadFile for background processing"""
    def __init__(self, path: str, filename: str, content_type: str):
        self.path = path
        self.filename = filename
        self.content_type = content_type
        self.file = open(path, "rb")

    def close(self):
        self.file.close()


async def _background_process_url(url: str, callback_url: str, trace_id: str):
    """Background task for URL processing"""
    try:
        # Run blocking service method in thread pool
        data = await asyncio.to_thread(_service.parse_url, url=url)
        await send_callback(callback_url, _ok(data), trace_id)
    except Exception as e:
        await send_callback(callback_url, _err(str(e)), trace_id)


async def _background_process_file(temp_path: str, filename: str, content_type: str, callback_url: str, trace_id: str):
    """Background task for file processing"""
    dummy_file = DummyUploadFile(temp_path, filename, content_type)
    try:
        # Run blocking service method in thread pool
        data = await asyncio.to_thread(_service.parse_file, file=dummy_file)
        await send_callback(callback_url, _ok(data), trace_id)
    except Exception as e:
        await send_callback(callback_url, _err(str(e)), trace_id)
    finally:
        dummy_file.close()
        # Clean up temp file
        try:
            os.unlink(temp_path)
        except OSError:
            pass


@router.post("/convert/url")
async def convert_url(
    payload: UrlRequest,
    background_tasks: BackgroundTasks
) -> dict:
    url = payload.url.strip()
    if not url:
        return _err("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _err("only http/https urls are supported")

    # Async callback mode
    if payload.callback_url:
        trace_id = get_trace_id() or generate_trace_id()
        background_tasks.add_task(
            _background_process_url,
            url,
            payload.callback_url,
            trace_id
        )
        return {"code": 0, "msg": "Task accepted", "data": {"trace_id": trace_id}}

    # Sync mode
    try:
        data = await asyncio.to_thread(_service.parse_url, url=url)
        return _ok(data)
    except ParserServiceError as exc:
        return _err(str(exc))


@router.post("/convert/file")
async def convert_file(
    file: UploadFile = File(...),
    callback_url: Optional[str] = Form(None),
    background_tasks: BackgroundTasks = BackgroundTasks()
) -> dict:
    if not file.filename:
        return _err("filename is required")

    # Async callback mode
    if callback_url:
        trace_id = get_trace_id() or generate_trace_id()

        # Save to temp file for background processing
        # Note: We must manage this temp file lifecycle manually
        fd, temp_path = tempfile.mkstemp()
        os.close(fd)

        try:
            with open(temp_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
        except Exception as e:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return _err(f"Failed to save upload file: {e}")
        finally:
            file.file.close()

        background_tasks.add_task(
            _background_process_file,
            temp_path,
            file.filename,
            file.content_type or "application/octet-stream",
            callback_url,
            trace_id
        )
        return {"code": 0, "msg": "Task accepted", "data": {"trace_id": trace_id}}

    # Sync mode
    try:
        # Note: ParserService is blocking, run in thread
        data = await asyncio.to_thread(_service.parse_file, file=file)
        return _ok(data)
    except ParserServiceError as exc:
        return _err(str(exc))
    finally:
        file.file.close()
