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
