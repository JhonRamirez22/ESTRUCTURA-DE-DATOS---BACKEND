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
