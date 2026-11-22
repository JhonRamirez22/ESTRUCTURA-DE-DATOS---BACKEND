"""Bounded cache whose entries also expire after a fixed time-to-live."""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable

from app.core.structures.lru_cache import LruCache


class TtlCache[K: Hashable, V]:
    """An :class:`LruCache` that forgets entries older than ``ttl_seconds``.

    Reads and writes stay O(1): the LRU keeps recency order and every value carries its own
    expiry timestamp, so an expired entry is simply treated as a miss.
    """

    def __init__(
        self,
        capacity: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        self._entries: LruCache[K, tuple[float, V]] = LruCache(capacity)
        self._ttl = ttl_seconds
        self._clock = clock

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            return None
        return value

    def put(self, key: K, value: V) -> None:
        self._entries.put(key, (self._clock() + self._ttl, value))

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
