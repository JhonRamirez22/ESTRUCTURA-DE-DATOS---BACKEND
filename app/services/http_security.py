"""Límites antes de parsear JSON y cabeceras comunes sin registrar entradas sensibles."""

from __future__ import annotations

import hmac
import logging
from time import perf_counter
from uuid import UUID, uuid4

from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import get_settings

MAX_REQUEST_BYTES = 65_536
# El balanceador comprueba la salud sin conocer el secreto de origen.
_ORIGIN_EXEMPT_PATHS = {"/api/health", "/api/v1/health"}
_inflight = 0  # peticiones en curso de este proceso
_LOGGER = logging.getLogger(__name__)


class HttpSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        global _inflight
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        exempt = scope["path"] in _ORIGIN_EXEMPT_PATHS
        if not exempt and _inflight >= get_settings().max_inflight_requests:
            # Carga por encima de la capacidad: se rechaza al instante para no acumular colas.
            _LOGGER.warning("http_overload reason=inflight")
            await JSONResponse(
                {"error": "El servicio está saturado; reintenta en unos segundos."},
                status_code=503,
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        _inflight += 1
        try:
            await self._dispatch(scope, receive, send)
        finally:
            _inflight -= 1

    async def _dispatch(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        try:
            request_id = str(UUID(headers.get("X-Request-ID", "")))
