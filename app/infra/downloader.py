from typing import Optional

import requests

from app.config import get_settings

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def fetch(
    url: str, *, timeout: Optional[int] = None
) -> requests.Response:
    session = _get_session()
    if timeout is None:
        timeout = get_settings().download.timeout_seconds
    return session.get(url, stream=True, timeout=timeout)
