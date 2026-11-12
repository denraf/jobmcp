from __future__ import annotations

import logging
import random
import threading
import time

import requests
from requests.adapters import HTTPAdapter

from . import config

log = logging.getLogger("jobmcp.http")

UA = {"User-Agent": "jobmcp/0.1 (+job-board inventory; contact via repo)"}
TIMEOUT = 30


def make_session(pool: int) -> requests.Session:
    """A session with its own connection pool. requests keeps only 10
    connections per host by default, so with more
    threads than that the extras are opened and thrown away, and urllib3 warns
    "Connection pool is full"."""
    s = requests.Session()
    s.headers.update(UA)
    for prefix in ("https://", "http://"):
        s.mount(prefix, HTTPAdapter(pool_connections=pool, pool_maxsize=pool))
    return s


# Vendor job syncing: shared by the sync threads. Common Crawl has a separate
# session (`crawl_session`) and gate, so the
# two kinds of traffic never share connections, and neither can exhaust the
# other's pool.
POOL = max(32, config.SYNC_WORKERS * 2)
_session = make_session(POOL)
crawl_session = make_session(max(8, config.CC_WORKERS * 2))


class Unavailable(requests.RequestException):
    """A gated host failed so many times in a row that the gate is open:
    callers should stop and try again later, not queue more requests."""


class Gate:
    """Shared politeness for one remote host, across all the threads that talk
    to it.

    Spaces requests at least `min_interval` seconds apart, and when the host
    starts failing (503/504, resets, refused
    connections: its way of saying "slow down") every thread backs off
    together, for longer each consecutive failure,
    instead of each thread hammering it on its own timer."""

    def __init__(
        self,
        name: str,
        min_interval: float = 1.0,
        max_cooldown: float = 180.0,
        trip_after: int = 0,
    ):
        self.name, self.min_interval, self.max_cooldown = (
            name,
            min_interval,
            max_cooldown,
        )
        self.trip_after = trip_after  # 0 = never give up
        self._lock, self._next_ok, self._failures = threading.Lock(), 0.0, 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next_ok)
            self._next_ok = at + self.min_interval
        if at > now:
            time.sleep(at - now)

    def ok(self) -> None:
        with self._lock:
            self._failures = 0

    def reset(self) -> None:
        self.ok()
        with self._lock:
            self._next_ok = 0.0

    def tripped(self) -> bool:
        """True after `trip_after` failures in a row with no success between:
        the host is down or has blocked us, and waiting longer won't help."""
        with self._lock:
            return bool(self.trip_after) and self._failures >= self.trip_after

    def failed(self, hint: float = 0.0) -> float:
        """Record a failure; returns how long everyone now waits (the host's
        Retry-After if larger)."""
        with self._lock:
            self._failures += 1
            cool = min(
                self.max_cooldown, max(hint, 5.0 * 2 ** min(self._failures, 6))
            )
            cool *= random.uniform(1.0, 1.25)
            self._next_ok = max(self._next_ok, time.monotonic() + cool)
            n = self._failures
        log.warning(
            "%s is struggling (%d failures in a row); all requests to it "
            "pause for %.0fs",
            self.name,
            n,
            cool,
        )
        return cool


def request(
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff: float = 2.0,
    timeout: float = TIMEOUT,
    gate: Gate | None = None,
    session: requests.Session | None = None,
    **kw,
) -> requests.Response:
    """requests with retry on 429/5xx and connection errors.

    4xx other than 429 are returned as-is so callers can treat 404 as
    "board is gone" rather than "try again".
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        if gate and gate.tripped():
            raise Unavailable(f"{gate.name} is unreachable; giving up for now")
        if gate:
            gate.wait()
        started = time.monotonic()
        try:
            r = (session or _session).request(
                method, url, timeout=timeout, **kw
            )
            log.debug(
                "%s %s -> %s in %.2fs (attempt %d)",
                method,
                url,
                r.status_code,
                time.monotonic() - started,
                attempt + 1,
            )
            if (
                r.status_code in (429, 500, 502, 503, 504)
                and attempt < retries
            ):
                wait = (
                    float(r.headers.get("Retry-After", 0) or 0)
                    or backoff * 2**attempt
                )
                wait = gate.failed(wait) if gate else min(wait, 60)
                log.warning(
                    "%s %s answered %s; retrying in %.0fs (attempt %d of %d)",
                    method,
                    url,
                    r.status_code,
                    wait,
                    attempt + 1,
                    retries + 1,
                )
                time.sleep(wait)
                continue
            if gate:
                gate.ok()
            return r
        except requests.RequestException as e:
            last = e
            log.warning(
                "%s %s failed: %s: %s (attempt %d of %d)",
                method,
                url,
                type(e).__name__,
                e,
                attempt + 1,
                retries + 1,
            )
            if attempt < retries:
                time.sleep(gate.failed() if gate else backoff * 2**attempt)
    assert last is not None
    raise last


def get(url: str, **kw) -> requests.Response:
    return request("GET", url, **kw)


def post(url: str, **kw) -> requests.Response:
    return request("POST", url, **kw)
