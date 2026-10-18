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
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser positivo y finito")
        if transport is not None and client is not None:
            raise ValueError("No se puede inyectar transport y client al mismo tiempo")

        configured_url = base_url or os.getenv("OSRM_API_URL") or "https://router.project-osrm.org"
        self._base_url = configured_url.strip().rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError("OSRM_API_URL debe comenzar con http:// o https://")
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> OsrmClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> OsrmMatrixResult:
        """Consulta Table; duraciones en minutos, distancias en metros y null sin conexión."""
        validate_coordinates(coordinates)
        encoded = _serialize_coordinates(coordinates)
        radiuses = _serialize_radiuses(len(coordinates))
        payload = await self._request(
            f"/table/v1/driving/{encoded}?annotations=duration,distance&radiuses={radiuses}"
        )
        if payload.get("code") != "Ok":
            detail = payload.get("message", payload.get("code", "respuesta desconocida"))
            raise OsrmClientError(f"OSRM table response was not successful: {detail}")

        duration_seconds = _read_matrix(payload.get("durations"), "durations")
        distances_meters = _read_matrix(payload.get("distances"), "distances")
        _validate_matrix_dimensions(duration_seconds, len(coordinates), "durations")
        _validate_matrix_dimensions(distances_meters, len(coordinates), "distances")
        return {
