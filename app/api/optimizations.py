"""Panel de optimizaciones del despachador: historial reciente y resumen con IA."""

from __future__ import annotations

import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.core.routing.cached_matrix_provider import CachedMatrixProvider
from app.core.routing.cached_route_provider import CachedRouteProvider
from app.core.routing.routing_errors import RoutingClientError
from app.db.session import get_session
from app.services.live_optimization import LiveOptimizationService
from app.services.optimization_feed import (
    mark_summary_requested,
    recent_events,
    recent_incidents,
    stats,
    summary_cooldown_remaining,
)
from app.services.request_limiter import SlidingWindowLimiter
from app.services.route_planner import get_routing_providers
from app.settings import get_settings

router = APIRouter(prefix="/optimizaciones", tags=["optimizaciones"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}
_SUMMARY_PROMPT = (
    "Eres analista de despacho de reparto en Pasto. Resume en español, en máximo cuatro frases, "
    "las optimizaciones recientes usando solo los datos recibidos. Indica cuántas propuestas se "
    "aplicaron, el ahorro estimado y qué conviene vigilar. No inventes calles, direcciones ni "
    "repartidores; las rutas se identifican solo con su alias."
)


_LIVE_LIMITER = SlidingWindowLimiter(limit=20, window_seconds=60.0)


@router.get("/en-vivo")
async def live_fleet_proposal(
    claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider], Depends(get_routing_providers)
    ],
) -> JSONResponse:
    """Re-optimises every active route and proposes changes; nothing is applied here."""
    if not get_settings().live_optimization_enabled:
        return JSONResponse({"enabled": False, "routes": []}, headers=_NO_STORE)
    subject = str(claims.get("accountId") or claims["role"])
    retry = _LIVE_LIMITER.retry_after(subject)
    if retry is not None:
        return JSONResponse(
            {"error": "Demasiadas consultas de optimización en vivo."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry)},
        )
    matrix_provider, route_provider = providers
    try:
        payload = await LiveOptimizationService(matrix_provider, route_provider).snapshot(session)
    except RoutingClientError:
        return JSONResponse(
            {"error": "El proveedor vial no respondió; se conserva el plan actual."},
            status_code=502,
            headers=_NO_STORE,
        )
    except SQLAlchemyError:
        return JSONResponse(
            {"error": "No se pudieron leer las rutas activas."}, status_code=500, headers=_NO_STORE
        )
    return JSONResponse(payload, headers=_NO_STORE)


@router.get("/recientes")
async def recent_optimizations(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, object]:
    current = stats()
    return {
        "events": recent_events(limit),
        "incidents": recent_incidents(10),
        "stats": {
            "proposals": current.proposals,
            "cacheHits": current.cache_hits,
            "applied": current.applied,
            "trafficSegments": current.traffic_segments,
            "savedMinutes": current.saved_minutes,
        },
        "summaryCooldownSeconds": round(summary_cooldown_remaining(), 1),
    }


@router.post("/resumen")
async def summarize_optimizations(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
) -> dict[str, object]:
    """Pide a Groq un resumen de las optimizaciones; no cambia ninguna ruta."""
    remaining = summary_cooldown_remaining()
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Espera {round(remaining)} s antes de pedir otro resumen.",
            headers=_NO_STORE,
        )
    settings = get_settings()
    key = (
