"""Callback utility for asynchronous task notifications."""

from typing import Any, Dict, Optional
import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Retry configuration: 3 attempts, exponential backoff starting at 1s
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.NetworkError, httpx.TimeoutException, httpx.HTTPStatusError)),
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
            # Don't retry client errors (4xx), only server errors (5xx) could be retried but logic above retries all status errors?
            # Actually, standard practice: retry on 5xx, maybe not on 4xx.
            # For simplicity, we currently retry on all HTTPStatusError.
            # Let's refine: if 4xx, maybe we shouldn't retry, but let's keep it simple for now as network glitches are main concern.
            logger.warning(f"Callback failed: {url} [trace_id={trace_id}] status={e.response.status_code} error={e}")
            raise
        except Exception as e:
            logger.warning(f"Callback error: {url} [trace_id={trace_id}] error={e}")
            raise
