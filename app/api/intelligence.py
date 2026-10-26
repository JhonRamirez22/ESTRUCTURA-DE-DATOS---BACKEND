"""Inteligencia para el despachador: agrupar pendientes y explicar rutas. Solo lectura."""

from __future__ import annotations

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
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.services.location_intelligence import (
    PendingLocation,
    explain_route,
    suggest_groups,
)
from app.services.route_planner import get_route_optimization

router = APIRouter(prefix="/inteligencia", tags=["inteligencia"])
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}
# Respuestas de la IA por conjunto de pendientes o ruta: evita repetir llamadas a Groq.
_GROUP_CACHE: LruCache[tuple[str, ...], dict[str, Any]] = LruCache(64)
_EXPLAIN_CACHE: LruCache[str, dict[str, Any]] = LruCache(128)


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
