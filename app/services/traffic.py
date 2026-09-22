"""Cliente TomTom con caché LRU, TTL y stale-if-error para tráfico vial."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache

import httpx

from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.routing.contracts import Coordinate
from app.core.structures.doubly_linked_list import DoublyLinkedList, DoublyLinkedNode
from app.core.traffic.tiles import validate_traffic_tile
from app.core.traffic.tomtom import TrafficData, parse_tomtom_flow_response
from app.settings import get_settings

_LOGGER = logging.getLogger(__name__)
TOMTOM_TRAFFIC_ENDPOINT = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
)
TRAFFIC_CACHE_TTL_SECONDS = 90
TRAFFIC_STALE_MAX_AGE_SECONDS = 10 * 60
TRAFFIC_CACHE_MAX_ENTRIES = 512
TRAFFIC_RATE_LIMIT_COOLDOWN_SECONDS = 60
MAX_CONCURRENT_TOMTOM_REQUESTS = 4
TRAFFIC_TILE_CACHE_MAX_ENTRIES = 64
MAX_TRAFFIC_TILE_BYTES = 512 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class TrafficServiceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TrafficTimeoutError(TrafficServiceError):
    pass


class TrafficRateLimitError(TrafficServiceError):
    pass


class TrafficProviderError(TrafficServiceError):
    pass


class TrafficNoCoverageError(TrafficProviderError):
    pass


@dataclass(frozen=True, slots=True)
class TrafficTile:
    content: bytes
    observed_at: datetime
    cache_status: str = "FRESH"


@dataclass(frozen=True, slots=True)
class _TileCacheEntry:
    key: str
