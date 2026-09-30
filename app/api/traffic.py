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
    session_fingerprint = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    retry_after = _RATE_LIMITER.retry_after(session_fingerprint)
    if retry_after is not None:
        return JSONResponse(
            content={"error": "Se alcanzó el límite temporal de consultas de tráfico."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry_after)},
        )

    try:
        data = await service.get_flow_segment((lon, lat))
    except ValueError as error:
        return _error(str(error), 400)
    except TrafficRateLimitError:
        return _error("TomTom alcanzó su límite de consultas; inténtalo más tarde.", 429)
    except TrafficTimeoutError:
        return _error("TomTom tardó demasiado en responder.", 504)
    except TrafficProviderError as error:
        _LOGGER.warning(
            "TomTom Traffic no pudo responder a la consulta",
            extra={"status_code": error.status_code},
        )
        return _error("TomTom Traffic no está disponible temporalmente.", 502)

    return JSONResponse(
        content={
            "currentSpeedKmh": data.current_speed_kmh,
            "freeFlowSpeedKmh": data.free_flow_speed_kmh,
            "currentTravelTimeSeconds": data.current_travel_time_seconds,
            "freeFlowTravelTimeSeconds": data.free_flow_travel_time_seconds,
            "confidence": data.confidence,
            "roadClosure": data.road_closure,
            "trafficRatio": data.traffic_ratio,
            "travelTimeMultiplier": data.travel_time_multiplier,
            "trafficLevel": data.traffic_level.value,
            "timestamp": data.timestamp.astimezone(UTC).isoformat(),
            "cacheStatus": data.cache_status,
        },
        headers=_NO_STORE,
    )


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": message},
        status_code=status_code,
        headers=_NO_STORE,
    )
