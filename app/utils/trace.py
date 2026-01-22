"""Context management for request tracing."""

import contextvars
import uuid
from typing import Optional

# Context variable to store trace_id for the current request
trace_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "trace_id", default=None
)


def get_trace_id() -> Optional[str]:
    """Get the current trace_id from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set the trace_id in the current context."""
    trace_id_var.set(trace_id)


def generate_trace_id() -> str:
    """Generate a new unique trace_id."""
    return uuid.uuid4().hex[:16]
