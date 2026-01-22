"""FastAPI middleware for request tracing."""

from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.utils.trace import generate_trace_id, get_trace_id, set_trace_id


class TraceMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace_id into each request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Get trace_id from header or generate new one
        trace_id = request.headers.get("x-trace-id") or generate_trace_id()

        # Set trace_id in context
        set_trace_id(trace_id)

        # Process request
        response = await call_next(request)

        # Add trace_id to response headers
        response.headers["x-trace-id"] = trace_id

        return response
