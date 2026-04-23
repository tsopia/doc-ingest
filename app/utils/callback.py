"""Callback utility for asynchronous task notifications."""

from typing import Any, Dict
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


def _should_retry_callback(exc: BaseException) -> bool:
    """仅重试网络错误、超时以及 5xx 服务端错误；4xx 客户端错误不重试。"""
    if isinstance(exc, (httpx.NetworkError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception(_should_retry_callback),
    reraise=True
)
async def send_callback(url: str, data: Dict[str, Any], trace_id: str) -> None:
    """
    Send callback notification with retry logic.

    Args:
        url: The callback URL
        data: The payload to send (matches standard API response format)
        trace_id: The trace ID for request tracking
    """
    headers = {
        "Content-Type": "application/json",
        "X-Trace-Id": trace_id
    }

    logger.info(f"Sending callback to {url} [trace_id={trace_id}]")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(url, json=data, headers=headers)
            response.raise_for_status()
            logger.info(f"Callback success: {url} [trace_id={trace_id}] status={response.status_code}")
        except httpx.HTTPStatusError as e:
            logger.warning(f"Callback failed: {url} [trace_id={trace_id}] status={e.response.status_code} error={e}")
            raise
        except Exception as e:
            logger.warning(f"Callback error: {url} [trace_id={trace_id}] error={e}")
            raise

