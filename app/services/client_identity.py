"""Identidad del cliente para los límites de tasa, sin confiar en lo que el cliente declara.

``X-Forwarded-For`` lo escribe cada proxy a la derecha del valor anterior. Lo que el cliente
antepone puede ser inventado; el último salto lo añade la infraestructura propia y no se puede
falsificar. Solo cuando el backend exige el secreto de origen (``ORIGIN_AUTH_SECRET``) todo el
tráfico pasa por el proxy de Vercel, que reemplaza la cabecera con la IP real: entonces el primer
valor es fiable y cada cliente tiene su propio contador.
"""

from __future__ import annotations

from starlette.requests import Request

from app.settings import get_settings

_MAX_KEY_LENGTH = 64


def client_key(request: Request) -> str:
    hops = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    hops = [hop for hop in hops if hop]
    if hops:
        locked = get_settings().origin_auth_secret is not None
        return (hops[0] if locked else hops[-1])[:_MAX_KEY_LENGTH]
    return request.client.host if request.client else "desconocido"
