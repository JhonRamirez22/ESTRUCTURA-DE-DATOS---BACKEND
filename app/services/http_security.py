"""Límites antes de parsear JSON y cabeceras comunes sin registrar entradas sensibles."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 65_536
_LOGGER = logging.getLogger(__name__)


class HttpSecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        try:
            request_id = str(UUID(headers.get("X-Request-ID", "")))
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope["path"]
        legacy = path.startswith("/api/") and not path.startswith("/api/v1/")

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers.setdefault("Cache-Control", "private, no-store")
                if legacy:
                    response_headers["X-API-Deprecated"] = "true"
            await send(message)

        async def reject(status: int, message: str) -> None:
            await JSONResponse(
                {"error": message, "requestId": request_id}, status_code=status,
            )(scope, receive, secure_send)

        try:
            length = int(headers.get("Content-Length", "0"))
        except ValueError:
            await reject(400, "Content-Length inválido.")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            await reject(413, "La petición excede el tamaño permitido.")
            return
        body = bytearray()
        while True:
            chunk = await receive()
            if chunk["type"] == "http.disconnect":
                return
            data = chunk.get("body", b"")
            if len(body) + len(data) > MAX_REQUEST_BYTES:
                await reject(413, "La petición excede el tamaño permitido.")
                return
            body.extend(data)
            if not chunk.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, secure_send)
        if legacy:
            # El retiro exige 30 días sin estos eventos; nunca registra URL, query ni cuerpo.
            _LOGGER.info("legacy_api_used method=%s request_id=%s", scope["method"], request_id)
