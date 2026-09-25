"""Cliente asíncrono para OSRM Table y Route, con duración en minutos."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Literal, TypedDict

import httpx

from app.core.routing.contracts import Coordinate, GeoJsonLineString, validate_coordinates
from app.core.routing.routing_errors import RecoverableRoutingError, RoutingClientError

OSRM_MAX_SNAP_DISTANCE_METERS = 50
_WAYPOINT_GEOMETRY_TOLERANCE_METERS = 25


class OsrmMatrixResult(TypedDict):
    durations_minutes: list[list[float | None]]
    distances_meters: list[list[float | None]]


class OsrmRouteResult(TypedDict):
    geometry: GeoJsonLineString
    geometry_provider: Literal["OSRM"]
    distance_meters: float
    duration_minutes: float
    snap_distances_meters: list[float]


class OsrmClientError(RoutingClientError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message, status, response_body)


class OsrmRateLimitError(OsrmClientError, RecoverableRoutingError):
    def __init__(self, response_body: str) -> None:
        super().__init__(
            f"OSRM rate limit exceeded (HTTP 429): {response_body}",
            status=429,
            response_body=response_body,
        )


class OsrmTimeoutError(OsrmClientError, RecoverableRoutingError):
    def __init__(self, timeout_seconds: float) -> None:
        timeout_ms = round(timeout_seconds * 1_000)
        super().__init__(f"OSRM request timed out after {timeout_ms} ms")
        self.timeout_seconds = timeout_seconds


class OsrmClient:
    """Reusable HTTP client; inject a transport or client for isolated tests."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
