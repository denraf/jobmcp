"""Tiny in-process sliding-window limiter for login/register. Not shared across
processes."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

import logging

from . import config

log = logging.getLogger("livejobs.ratelimit")

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def auth_limit(request: Request) -> None:
    key, now = client_ip(request), time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.AUTH_RATE_PER_MIN:
            log.warning(
                "rate limit hit", extra={"ip": key, "path": request.url.path}
            )
            raise HTTPException(
                429,
                "too many attempts; wait a minute and try again",
                headers={"Retry-After": "60"},
            )
        q.append(now)


def reset() -> None:
    with _lock:
        _hits.clear()
