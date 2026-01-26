import sys

from fastapi import FastAPI
from loguru import logger

from app.api.routes import router as api_router
from app.config import get_settings
from app.middleware.trace import TraceMiddleware
from app.utils.trace import get_trace_id


import logging



class InterceptHandler(logging.Handler):
    """
    Intercept standard logging and redirect to Loguru with correct caller info.
    """
    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        # Use the original caller info from LogRecord instead of stack inspection
        # This ensures we show the actual source of the log, not the logging module
        logger.patch(lambda r: r.update(
            name=record.name,
            function=record.funcName,
            file=record.pathname,
            line=record.lineno,
        )).opt(exception=record.exc_info).log(level, record.getMessage())




def _format_record(record: dict) -> str:
    """
    Custom log format that includes trace_id.

    Format: {time} | {level} | [trace_id={trace_id}] | {name}:{function}:{line} - {message}
    """
    trace_id = get_trace_id() or "no-trace"

    format_string = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>[trace_id={extra[trace_id]}]</cyan> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>\n"
    )

    # Inject trace_id into extra fields
    record["extra"]["trace_id"] = trace_id

    return format_string


def _configure_logging() -> None:
    level = get_settings().log.level.upper()
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=_format_record,
    )

    # Intercept everything from standard logging
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Suppress verbose pdfminer logs
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

_configure_logging()

app = FastAPI()

# Add trace middleware
app.add_middleware(TraceMiddleware)

# Include routers
app.include_router(api_router)

# Import and include SSE routes
from app.api.sse_routes import router as sse_router
app.include_router(sse_router)


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时刷新 Langfuse 数据"""
    try:
        from langfuse import Langfuse
        langfuse = Langfuse()
        langfuse.flush()
        logger.info("Langfuse data flushed on shutdown")
    except Exception as e:
        logger.debug(f"Langfuse flush on shutdown skipped: {e}")
