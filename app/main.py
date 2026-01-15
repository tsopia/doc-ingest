import sys

from fastapi import FastAPI
from loguru import logger

from app.api.routes import router as api_router
from app.config import get_settings


def _configure_logging() -> None:
    level = get_settings().log.level.upper()
    logger.remove()
    logger.add(sys.stderr, level=level)


_configure_logging()

app = FastAPI()
app.include_router(api_router)
