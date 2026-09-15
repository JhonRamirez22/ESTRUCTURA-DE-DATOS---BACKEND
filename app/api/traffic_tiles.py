"""Teselas transparentes privadas: nunca entrega la clave TomTom al navegador."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.auth import _configured_secret, get_optional_session
from app.core.auth import SessionClaims
from app.core.traffic.tiles import validate_traffic_tile
from app.db.session import get_session
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.traffic import (
    TRAFFIC_CACHE_TTL_SECONDS,
    TrafficNoCoverageError,
    TrafficRateLimitError,
    TrafficService,
    TrafficServiceError,
    TrafficTimeoutError,
    get_traffic_service,
)
from app.settings import get_settings

router = APIRouter(prefix="/trafico", tags=["tráfico"])
_LIMITER = CustomerChatRateLimiter(max_requests=180, window_seconds=60)


def _authorize(claims: SessionClaims | None) -> SessionClaims:
    if claims is None:
        raise HTTPException(401, "Debes iniciar sesión para consultar el tráfico.")
    return claims


@router.get("/estado")
async def traffic_status(
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    service: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> dict[str, object]:
    _authorize(claims)
    tile = service.last_tile if service else None
    age = (datetime.now(UTC) - tile.observed_at).total_seconds() if tile else None
    return {
        "configured": service is not None,
        "state": "unavailable"
        if service is None
        else "not-consulted"
        if tile is None
        else "stale"
        if tile.cache_status == "STALE" or (age is not None and age >= TRAFFIC_CACHE_TTL_SECONDS)
        else "consulted",
        "observedAt": tile.observed_at.isoformat() if tile else None,
        "coverage": "partial-or-unknown",
        "refreshSeconds": TRAFFIC_CACHE_TTL_SECONDS,
    }
