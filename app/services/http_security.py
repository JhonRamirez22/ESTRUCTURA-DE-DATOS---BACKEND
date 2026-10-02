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
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope["path"]
        legacy = path.startswith("/api/") and not path.startswith("/api/v1/")
        started_at = perf_counter()
        response_started = False

        async def secure_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers.setdefault("Cache-Control", "private, no-store")
                forwarded_proto = headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
                if scope.get("scheme") == "https" or forwarded_proto == "https":
                    response_headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
                if legacy:
                    response_headers["X-API-Deprecated"] = "true"
                if message["status"] >= 400:
                    # La plantilla evita registrar IDs, direcciones y guías de la URL real.
                    _LOGGER.warning(
                        "http_failure request_id=%s method=%s route=%s status=%s duration_ms=%.0f",
                        request_id, scope["method"],
                        getattr(scope.get("route"), "path", "unmatched"),
                        message["status"], (perf_counter() - started_at) * 1000,
                    )
            await send(message)

        async def reject(status: int, message: str, retry_after: int | None = None) -> None:
            await JSONResponse(
                {"error": message, "requestId": request_id}, status_code=status,
                headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
            )(scope, receive, secure_send)

        needs_origin = path.startswith("/api") and path not in _ORIGIN_EXEMPT_PATHS
        if needs_origin and not _origin_allowed(headers):
            await reject(403, "Origen no autorizado.")
            return
        try:
            length = int(headers.get("Content-Length", "0"))
        except ValueError:
            await reject(400, "Content-Length inválido.")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
