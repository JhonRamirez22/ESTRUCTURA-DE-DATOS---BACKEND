"""Teselas transparentes privadas: nunca entrega la clave TomTom al navegador."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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


@router.get("/tiles/{z}/{x}/{y}.png")
async def traffic_tile(
    z: int,
    x: int,
    y: int,
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    service: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> Response:
    identity = _authorize(claims)
    try:
        validate_traffic_tile(z, x, y)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    secret = _configured_secret(get_settings())
    if service is None or secret is None:
        raise HTTPException(503, "TomTom Traffic no está configurado.")
    retry = await _LIMITER.retry_after_shared(
        session,
        f"traffic-tiles:{identity['accountId']}",
        hmac_secret=secret,
    )
    if retry is not None:
        raise HTTPException(
            429, "Límite temporal de consultas de tráfico.", headers={"Retry-After": str(retry)}
        )
    try:
        tile = await service.get_flow_tile(z, x, y)
    except TrafficNoCoverageError as error:
        raise HTTPException(404, "Sin cobertura de tráfico para esta área.") from error
    except TrafficRateLimitError as error:
        raise HTTPException(
            429, "TomTom alcanzó su límite.", headers={"Retry-After": "90"}
        ) from error
    except TrafficTimeoutError as error:
        raise HTTPException(504, "TomTom tardó demasiado al consultar el mapa.") from error
    except TrafficServiceError as error:
        raise HTTPException(502, "La capa de tráfico no está disponible temporalmente.") from error
    age = (datetime.now(UTC) - tile.observed_at).total_seconds()
    max_age = max(0, int(TRAFFIC_CACHE_TTL_SECONDS - age)) if tile.cache_status == "FRESH" else 0
    return Response(
        tile.content,
        media_type="image/png",
        headers={
            "Cache-Control": f"private, max-age={max_age}",
            "X-Traffic-Observed-At": tile.observed_at.isoformat(),
            "X-Traffic-Cache": tile.cache_status,
            "X-Traffic-Coverage": "partial-or-unknown",
        },
    )
