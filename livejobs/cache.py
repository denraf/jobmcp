"""A small in-process cache for answers that are the same for everybody
(the public jobs list). Entries expire after a few seconds; while one request
is computing an answer, identical requests wait for it instead of each running
the same query.

It is per process and in memory: nothing is stored on disk, and there is
nothing to invalidate, because a short expiry is enough (jobs change hourly).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Hashable

from . import config


class TTLCache:
    def __init__(self, maxsize: int = 500):
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, tuple[float, object]] = OrderedDict()
        self._locks: dict[Hashable, threading.Lock] = {}
        self._guard = threading.Lock()
        self.hits = self.misses = 0

    def _fresh(self, key, now):
        item = self._data.get(key)
        if item and item[0] > now:
            self._data.move_to_end(key)
            return item
        return None

    def get_or_compute(self, key: Hashable, compute: Callable, ttl: float):
        """The cached value for `key`, or `compute()` stored for `ttl`
        seconds. ttl <= 0 turns caching off."""
        if ttl <= 0:
            return compute()
        with self._guard:
            item = self._fresh(key, time.monotonic())
            if item:
                self.hits += 1
                return item[1]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # one computation per key; the others wait here
            with self._guard:
                item = self._fresh(key, time.monotonic())
                if item:
                    self.hits += 1
                    return item[1]
                self.misses += 1
            try:
                value = compute()
            finally:
                with self._guard:
                    self._locks.pop(key, None)
            with self._guard:
                self._data[key] = (time.monotonic() + ttl, value)
                while len(self._data) > self.maxsize:
                    self._data.popitem(last=False)
            return value

    def clear(self) -> None:
        with self._guard:
            self._data.clear()


public = TTLCache()


def ttl() -> float:
    return config.CACHE_SECONDS
