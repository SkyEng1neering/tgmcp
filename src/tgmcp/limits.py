"""Load management: a bounded queue for tool calls and a single-flight TTL cache for Telegram reads.

All clients share one Telegram session (a session cannot safely be used from several processes), so instead of
scaling out we keep one async process and make it degrade gracefully: excess calls wait in a FIFO queue, calls that
wait too long get a clear "busy" error, and identical reads issued by different clients hit Telegram only once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)


class BusyError(Exception):
    """The server is saturated; the caller should retry later."""


class CallGate:
    """At most `limit` calls run at once; up to `max_queue` more wait (FIFO) for at most `timeout` seconds."""

    def __init__(self, limit: int, timeout: float, max_queue: int):
        self._sem = asyncio.Semaphore(limit)
        self.limit = limit
        self.timeout = timeout
        self.max_queue = max_queue
        self.running = 0
        self.waiting = 0

    @asynccontextmanager
    async def slot(self):
        if self._sem.locked() and self.waiting >= self.max_queue:
            raise BusyError(f"server busy: {self.running} calls running, {self.waiting} queued; retry shortly")
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), self.timeout)
        except asyncio.TimeoutError:
            raise BusyError(f"server busy: waited {self.timeout:.0f}s in the queue; retry shortly") from None
        finally:
            self.waiting -= 1
        self.running += 1
        try:
            yield
        finally:
            self.running -= 1
            self._sem.release()

    def stats(self) -> dict[str, int]:
        return {"running": self.running, "queued": self.waiting, "limit": self.limit}


class TTLCache:
    """Async memoiser: fresh results are reused for `ttl` seconds and concurrent identical calls share one request."""

    def __init__(self, ttl: float, max_entries: int = 2000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0

    async def get(self, key: Hashable, factory: Callable[[], Awaitable[Any]]) -> Any:
        if self.ttl <= 0:
            return await factory()
        now = time.monotonic()
        item = self._data.get(key)
        if item and item[0] > now:
            self._data.move_to_end(key)
            self.hits += 1
            return item[1]
        if key in self._inflight:
            self.hits += 1
            return await asyncio.shield(self._inflight[key])
        self.misses += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await factory()
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
                fut.exception()  # mark retrieved so lone failures don't log "never retrieved"
            raise
        else:
            fut.set_result(value)
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
            return value
        finally:
            self._inflight.pop(key, None)
