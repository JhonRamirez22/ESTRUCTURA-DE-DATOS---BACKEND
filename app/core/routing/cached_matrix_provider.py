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
    return f"{_MATRIX_CACHE_SCHEMA}:{source}:{rounded!r}"


def clear_matrix_cache() -> None:
    """Limpia el caché de módulo para pruebas; no se necesita en producción."""
    while _matrix_cache.pop_back() is not None:
        pass
    _matrix_cache_nodes.clear()


def _copy_matrix_result(result: MatrixResult) -> MatrixResult:
    copied: MatrixResult = {
        "durations_minutes": [list(row) for row in result["durations_minutes"]],
    }
    if "distances_meters" in result:
        copied["distances_meters"] = [list(row) for row in result["distances_meters"]]
    return copied


def _copy_result(result: MatrixResult, *, source: MatrixSource) -> MatrixProviderResult:
    copied = _copy_matrix_result(result)
    provider_result: MatrixProviderResult = {
        "durations_minutes": copied["durations_minutes"],
        "source": source,
    }
    if "distances_meters" in copied:
        provider_result["distances_meters"] = copied["distances_meters"]
    return provider_result


def _fallback_warning(error: Exception, source: MatrixSource) -> str:
    provider = source.upper()
    if "RateLimit" in error.__class__.__name__:
        reason = "alcanzó el límite de solicitudes"
    else:
        reason = "agotó el tiempo de espera"
    return f"{provider} {reason}; se usó una matriz euclidiana aproximada."


def _build_euclidean_fallback_matrix(
    coordinates: Sequence[Coordinate],
) -> list[list[float | None]]:
    meters_per_minute = FALLBACK_MAX_SPEED_KMH * _METERS_PER_KILOMETER / _MINUTES_PER_HOUR
    return [
        [_fallback_minutes(source, target, meters_per_minute) for target in coordinates]
        for source in coordinates
    ]


def _fallback_minutes(
    source: Coordinate,
    target: Coordinate,
    meters_per_minute: float,
) -> float:
    source_lng, source_lat = source
    target_lng, target_lat = target
    mean_latitude = math.radians((source_lat + target_lat) / 2)
    delta_x = (target_lng - source_lng) * _METERS_PER_DEGREE_LATITUDE * math.cos(mean_latitude)
    delta_y = (target_lat - source_lat) * _METERS_PER_DEGREE_LATITUDE
    return math.hypot(delta_x, delta_y) / meters_per_minute
