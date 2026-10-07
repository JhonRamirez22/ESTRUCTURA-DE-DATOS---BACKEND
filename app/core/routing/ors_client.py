"""Adaptador alternativo asíncrono de OpenRouteService (Matrix + Directions)."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence

import httpx

from app.core.routing.contracts import (
    Coordinate,
    GeoJsonLineString,
    MatrixResult,
    RouteResult,
    validate_coordinates,
)
from app.core.routing.routing_errors import RecoverableRoutingError, RoutingClientError


class OrsClientError(RoutingClientError):
    pass


class OrsRateLimitError(OrsClientError, RecoverableRoutingError):
    def __init__(self, response_body: str) -> None:
        super().__init__(
            f"OpenRouteService rate limit exceeded (HTTP 429): {response_body}",
            status=429,
            response_body=response_body,
        )


class OrsTimeoutError(OrsClientError, RecoverableRoutingError):
    def __init__(self, timeout_seconds: float) -> None:
        timeout_ms = round(timeout_seconds * 1_000)
        super().__init__(f"OpenRouteService request timed out after {timeout_ms} ms")
        self.timeout_seconds = timeout_seconds


class OrsClient:
    """Usa ORS solo si se configura explícitamente; nunca requiere red en tests."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        configured_key = api_key if api_key is not None else os.getenv("ORS_API_KEY")
        if not configured_key or not configured_key.strip():
            raise ValueError("ORS_API_KEY debe definirse antes de construir OrsClient")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds debe ser positivo y finito")
        if transport is not None and client is not None:
            raise ValueError("No se puede inyectar transport y client al mismo tiempo")

        configured_url = base_url or os.getenv("ORS_API_URL") or "https://api.openrouteservice.org"
        self._base_url = configured_url.strip().rstrip("/")
        if not self._base_url.startswith(("http://", "https://")):
            raise ValueError("ORS_API_URL debe comenzar con http:// o https://")
        self._api_key = configured_key.strip()
        self._timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> OrsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_duration_matrix(self, coordinates: Sequence[Coordinate]) -> MatrixResult:
        """ORS entrega segundos; esta función los normaliza a minutos."""
        validate_coordinates(coordinates)
        payload = await self._request(
            "/v2/matrix/driving-car",
            {"locations": coordinates, "metrics": ["duration"]},
        )
        durations_seconds = _read_matrix(payload.get("durations"), len(coordinates))
        return {
            "durations_minutes": [
                [None if seconds is None else seconds / 60 for seconds in row]
                for row in durations_seconds
            ]
        }

    async def get_route(self, coordinates: Sequence[Coordinate]) -> RouteResult:
        """Retorna geometría vial en [lng,lat], distancia en metros y minutos."""
        validate_coordinates(coordinates)
        payload = await self._request(
            "/v2/directions/driving-car/geojson",
            {"coordinates": coordinates},
        )
        features = payload.get("features")
        if not isinstance(features, list) or not features:
            raise OrsClientError("OpenRouteService directions response has no features")
        feature = _read_object(
            features[0], "OpenRouteService directions response has an invalid feature"
        )
        geometry = _read_geometry(feature.get("geometry"))
        properties = _read_object(
            feature.get("properties"),
            "OpenRouteService directions response has invalid properties",
        )
        summary = _read_object(
            properties.get("summary"),
            "OpenRouteService directions response has no summary",
        )
        distance = _read_number(summary.get("distance"), "distance")
        duration_seconds = _read_number(summary.get("duration"), "duration")
        return {
            "geometry": geometry,
            "geometry_provider": "ORS",
            "distance_meters": distance,
            "duration_minutes": duration_seconds / 60,
        }

    async def _request(self, path: str, body: object) -> dict[str, object]:
        try:
            response = await self._client.post(
                path,
                json=body,
                headers={"Authorization": self._api_key},
            )
        except httpx.TimeoutException as error:
            raise OrsTimeoutError(self._timeout_seconds) from error
        except httpx.RequestError as error:
            raise OrsClientError(f"OpenRouteService network request failed: {error}") from error

        if not response.is_success:
            response_body = response.text
            if response.status_code == 429:
                raise OrsRateLimitError(response_body)
            raise OrsClientError(
                f"OpenRouteService request failed (HTTP {response.status_code}): {response_body}",
                status=response.status_code,
                response_body=response_body,
            )

        try:
            raw_payload: object = response.json()
