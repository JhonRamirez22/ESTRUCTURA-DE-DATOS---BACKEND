"""Inteligencia para el despachador: agrupar pendientes y explicar rutas. Solo lectura."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.core.structures.lru_cache import LruCache
from app.db.session import get_session
from app.repositories.courier_repository import CourierRepository
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.services.dispatch_suggestions import (
    CourierView,
    PendingView,
    build_suggestions,
    phrase_with_ai,
)
from app.services.location_intelligence import (
    PendingLocation,
    explain_route,
    suggest_groups,
)
from app.services.request_limiter import SlidingWindowLimiter
from app.services.route_planner import get_route_optimization
from app.settings import get_settings

router = APIRouter(prefix="/inteligencia", tags=["inteligencia"])
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}
# Respuestas de la IA por conjunto de pendientes o ruta: evita repetir llamadas a Groq.
_GROUP_CACHE: LruCache[tuple[str, ...], dict[str, Any]] = LruCache(64)
_EXPLAIN_CACHE: LruCache[str, dict[str, Any]] = LruCache(128)
_SUGGESTION_CACHE: LruCache[str, tuple[list[dict[str, Any]], dict[str, Any]]] = LruCache(64)
_SUGGESTION_LIMITER = SlidingWindowLimiter(limit=30, window_seconds=60.0)


async def cached_groups(
    key: tuple[str, ...], locations: list[PendingLocation]
) -> dict[str, Any]:
    """Grupos con caché, pero solo si la IA respondió: un fallo pasajero no se recuerda."""
    cached = _GROUP_CACHE.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    result = await suggest_groups(locations)
    ai = result["ai"]
    if isinstance(ai, dict) and ai.get("proposed"):
        _GROUP_CACHE.put(key, result)
    return {**result, "cached": False}


async def cached_explanation(key: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Explicación con caché solo si es texto de Groq validado, no el de respaldo."""
    cached = _EXPLAIN_CACHE.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    result = await explain_route(summary)
    if result.get("source") == "groq-validated":
        _EXPLAIN_CACHE.put(key, result)
    return {**result, "cached": False}


@router.get("/agrupaciones")
async def suggest_order_groups(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    try:
        pending = await DeliveryPointRepository(session).list_pending_unassigned()
    except SQLAlchemyError:
        return JSONResponse({"error": "No se pudieron leer los pendientes."}, status_code=500)
    locations = [
        PendingLocation(ident=str(point.id), alias=f"p-{index}", lat=point.lat, lng=point.lng)
        for index, point in enumerate(pending)
    ]
    cache_key = tuple(sorted(f"{loc.ident}:{loc.lat:.5f}:{loc.lng:.5f}" for loc in locations))
    result = await cached_groups(cache_key, locations)
    by_alias = {loc.alias: loc.ident for loc in locations}
    groups = [
        {
            "ids": [by_alias[alias] for alias in group["members"]],
            "spreadMeters": group["spreadMeters"],
            "source": group["source"],
            "reason": group["reason"],
        }
        for group in result["groups"]
    ]
    payload = {"groups": groups, "ai": result["ai"], "cached": result["cached"]}
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/rutas/{route_id}/explicar")
async def explain_optimization(
    route_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
) -> JSONResponse:
    try:
        parsed = UUID(route_id)
    except ValueError:
        return JSONResponse({"error": "La ruta no existe."}, status_code=404)
    summary = get_route_optimization(parsed)
    if summary is None:
        return JSONResponse(
            {"error": "Esta ruta aún no tiene resumen de optimización."}, status_code=404
        )
    key = f"{route_id}:{summary.get('computedAt')}"
    return JSONResponse(await cached_explanation(key, dict(summary)), headers=_NO_STORE)


@router.get("/sugerencias")
async def dispatch_suggestions(
    claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    if not get_settings().ai_suggestions_enabled:
        return JSONResponse(
            {"enabled": False, "suggestions": [], "ai": {"source": "deterministic"}},
            headers=_NO_STORE,
        )
    subject = claims.get("accountId") or claims["role"]
    retry = _SUGGESTION_LIMITER.retry_after(hashlib.sha256(subject.encode()).hexdigest())
    if retry is not None:
        return JSONResponse(
            {"error": "Demasiadas consultas de sugerencias."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry)},
        )
    try:
        couriers = await CourierRepository(session).list_couriers()
        pending = await DeliveryPointRepository(session).list_pending_unassigned()
    except SQLAlchemyError:
        return JSONResponse({"error": "No se pudo leer la operación."}, status_code=500)
    now = datetime.now(UTC).replace(tzinfo=None)
    cards = build_suggestions(
        [
            CourierView(
                str(c.id), c.name, c.status, c.current_lat, c.current_lng, c.last_location_at
            )
            for c in couriers
        ],
        [PendingView(str(p.id), p.lat, p.lng) for p in pending],
        now,
    )
    key = hashlib.sha256(
        json.dumps(
            [(c["id"], c["text"], c["figures"]) for c in cards], sort_keys=True
        ).encode()
    ).hexdigest()
    cached = _SUGGESTION_CACHE.get(key)
    if cached is not None:
        public, ai = cached
        return JSONResponse(
            {"enabled": True, "suggestions": public, "ai": {**ai, "cached": True}},
            headers=_NO_STORE,
        )
    public, ai = await phrase_with_ai(cards)
    if ai.get("source") == "groq-validated":
        _SUGGESTION_CACHE.put(key, (public, ai))
    return JSONResponse(
        {"enabled": True, "suggestions": public, "ai": {**ai, "cached": False}},
        headers=_NO_STORE,
    )
