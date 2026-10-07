"""Caché LRU de matrices viales con fallback geométrico explícito."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Literal, NotRequired, Protocol, TypedDict

from app.core.routing.contracts import Coordinate, MatrixResult, validate_coordinates
from app.core.routing.routing_errors import is_recoverable_routing_error
from app.core.structures.doubly_linked_list import DoublyLinkedList, DoublyLinkedNode

MATRIX_CACHE_TTL_SECONDS = 5 * 60
MATRIX_CACHE_MAX_ENTRIES = 100
# Techo prudente para Pasto: pendientes fuertes y calles estrechas del centro
# hacen poco realista asumir velocidades altas; no representa tráfico medido.
FALLBACK_MAX_SPEED_KMH = 30
_MATRIX_CACHE_SCHEMA = "road-snap-50m-v2"
_METERS_PER_KILOMETER = 1_000
_MINUTES_PER_HOUR = 60
_METERS_PER_DEGREE_LATITUDE = 111_320
type MatrixSource = Literal["osrm", "ors"]


class MatrixClient(Protocol):
    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> MatrixResult: ...


class MatrixProviderResult(TypedDict):
    durations_minutes: list[list[float | None]]
    source: Literal["osrm", "ors", "euclidean-fallback"]
    distances_meters: NotRequired[list[list[float | None]]]
    warning: NotRequired[str]


class _CacheEntry(TypedDict):
    key: str
    expires_at: float
    result: MatrixResult


# La lista conserva MRU al frente y LRU al final; el mapa permite mover nodos en O(1).
# Esta caché es local al proceso y puede reiniciarse con hot reload; no es persistente.
_matrix_cache: DoublyLinkedList[_CacheEntry] = DoublyLinkedList()
_matrix_cache_nodes: dict[str, DoublyLinkedNode[_CacheEntry]] = {}


class CachedMatrixProvider:
    def __init__(
        self,
        client: MatrixClient,
        *,
        source: MatrixSource = "osrm",
        ttl_seconds: float = MATRIX_CACHE_TTL_SECONDS,
        max_entries: int = MATRIX_CACHE_MAX_ENTRIES,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds debe ser positivo y finito")
        if max_entries < 1:
            raise ValueError("max_entries debe ser positivo")
        self._client = client
        self._source = source
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._now = now

    async def get_duration_matrix(
        self,
        coordinates: Sequence[Coordinate],
    ) -> MatrixProviderResult:
        validate_coordinates(coordinates)
        key = build_matrix_cache_key(coordinates, source=self._source)
        cached_node = _matrix_cache_nodes.get(key)
        current_time = self._now()
        if cached_node is not None and cached_node.value["expires_at"] > current_time:
            _matrix_cache.move_to_front(cached_node)
            cached = cached_node.value
            return _copy_result(cached["result"], source=self._source)
        if cached_node is not None:
            _matrix_cache.remove(cached_node)
            del _matrix_cache_nodes[key]

        # TODO: deduplicar miss simultáneos cuando el volumen supere el MVP de un despachador.
        try:
            result = await self._client.get_duration_matrix(coordinates)
        except Exception as error:
            if not is_recoverable_routing_error(error):
                raise
            return {
                "durations_minutes": _build_euclidean_fallback_matrix(coordinates),
                "source": "euclidean-fallback",
                "warning": _fallback_warning(error, self._source),
            }

        existing_node = _matrix_cache_nodes.get(key)
        if existing_node is not None:
            _matrix_cache.remove(existing_node)
        entry: _CacheEntry = {
            "key": key,
            "expires_at": self._now() + self._ttl_seconds,
            "result": _copy_matrix_result(result),
        }
        _matrix_cache_nodes[key] = _matrix_cache.appendleft(entry)
        while len(_matrix_cache) > self._max_entries:
            evicted = _matrix_cache.pop_back()
            if evicted is not None:
                _matrix_cache_nodes.pop(evicted["key"], None)
        return _copy_result(result, source=self._source)


def build_matrix_cache_key(
    coordinates: Sequence[Coordinate],
    *,
    source: MatrixSource = "osrm",
) -> str:
    """Conserva orden y redondea a 5 decimales para amortiguar jitter GPS."""
    rounded = [[round(longitude, 5), round(latitude, 5)] for longitude, latitude in coordinates]
