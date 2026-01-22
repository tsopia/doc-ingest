from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from app.services.parser_service import ParserService, ParserServiceError
from app.utils.parse_utils import parse_structured

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
    structured: Optional[list[str]] = None
    keep_data_uris: bool = True
    extract_images: bool = True


def _ok(data: dict) -> dict:
    return {"code": 0, "data": data, "msg": ""}


def _err(message: str, code: int = 1) -> dict:
    return {"code": code, "data": {}, "msg": message}


@router.post("/convert/url")
def convert_url(payload: UrlRequest) -> dict:
    url = payload.url.strip()
    if not url:
        return _err("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _err("only http/https urls are supported")

    structured, err = parse_structured(payload.structured)
    if err:
        return _err(err)

    try:
        data = _service.parse_url(
            url=url,
            structured=structured,
            keep_data_uris=payload.keep_data_uris,
            extract_images=payload.extract_images,
        )
        return _ok(data)
    except ParserServiceError as exc:
        return _err(str(exc))


@router.post("/convert/file")
def convert_file(
    file: UploadFile = File(...),
    structured: Optional[str] = Form(None),
    keep_data_uris: bool = Form(True),
    extract_images: bool = Form(True),
) -> dict:
    if not file.filename:
        return _err("filename is required")

    structured, err = parse_structured(structured)
    if err:
        return _err(err)

    try:
        data = _service.parse_file(
            file=file,
            structured=structured,
            keep_data_uris=keep_data_uris,
            extract_images=extract_images,
        )
        return _ok(data)
    except ParserServiceError as exc:
        return _err(str(exc))
    finally:
        file.file.close()
