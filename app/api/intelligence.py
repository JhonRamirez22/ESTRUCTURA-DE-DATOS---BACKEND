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
from app.models.enums import DeliveryPointStatus
from app.repositories.courier_repository import CourierRepository
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.repositories.route_repository import RouteRepository
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
from app.services.shift_report import RouteView, build_shift_cards
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
