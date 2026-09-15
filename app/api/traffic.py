"""Consulta autenticada de estado vial TomTom dentro de Pasto."""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from starlette.responses import JSONResponse

from app.api.auth import get_optional_session
from app.core.auth import SessionClaims
from app.core.location.pasto_area import is_within_pasto_service_area
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.traffic import (
    TrafficProviderError,
    TrafficRateLimitError,
    TrafficService,
    TrafficTimeoutError,
    get_traffic_service,
)

router = APIRouter(prefix="/traffic", tags=["tráfico"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}
_RATE_LIMITER = CustomerChatRateLimiter(max_requests=60, window_seconds=60)


@router.get("")
async def get_traffic(
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    service: Annotated[TrafficService | None, Depends(get_traffic_service)],
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para consultar el tráfico.", 401)
    if not math.isfinite(lat) or not math.isfinite(lon):
        return _error("lat y lon deben ser coordenadas finitas.", 400)
    if not is_within_pasto_service_area(lat, lon):
        return _error("La consulta de tráfico solo está disponible dentro de Pasto.", 400)
    if service is None:
        return _error("TomTom Traffic no está configurado todavía.", 503)

    # Cada cuenta tiene su propio contador; la cookie ya no es única, así que se usa la cuenta.
    subject = claims.get("accountId") or claims.get("courierId") or claims["role"]
