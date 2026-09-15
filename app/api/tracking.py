"""Seguimiento público por guía, con ubicación y ruta vial minimizadas."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.core.location.courier_location import is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.metrics.estimate_eta import StopEta, estimate_stop_etas_from_matrix
from app.core.routing.cached_matrix_provider import CachedMatrixProvider, MatrixProviderResult
from app.core.routing.cached_route_provider import CachedRouteProvider
from app.core.routing.contracts import GeoJsonLineString, RouteResult
from app.db.session import get_session
from app.models import DeliveryPointStatus, RouteStatus
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.customer_eta_traffic import (
    FirstLegTrafficEstimate,
    estimate_first_leg_with_traffic,
)
from app.services.delivery_history import DeliveryHistoryReader
from app.services.route_planner import get_routing_providers
from app.services.traffic import TrafficService, get_traffic_service
from app.settings import get_settings

router = APIRouter(prefix="/seguimiento", tags=["seguimiento"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store, private"}
# Acota el consumo de ETA/tráfico (TomTom) por guía: 30 consultas por minuto.
_TRACKING_LIMITER = CustomerChatRateLimiter(max_requests=30, window_seconds=60)
_TERMINAL_POINT_STATUSES = (DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED)
_ACTIVE_ROUTE_STATUSES = (RouteStatus.PLANNED, RouteStatus.IN_PROGRESS)


@router.get("")
async def get_tracking(
    session: Annotated[AsyncSession, Depends(get_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
    traffic_provider: Annotated[TrafficService | None, Depends(get_traffic_service)],
    guide: Annotated[str | None, Query(alias="guia")] = None,
) -> JSONResponse:
    normalized_guide = guide.strip() if guide is not None else ""
    if not normalized_guide:
        return _error("Ingresa el número de guía para consultar el pedido.", 400)

    limited = await _tracking_retry_after(session, normalized_guide)
    if limited is not None:
        return JSONResponse(
            content={"error": "Demasiadas consultas seguidas. Espera un momento."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(limited)},
        )

    matrix_provider, route_provider = providers
    try:
        delivery_point = await DeliveryPointRepository(
            session
        ).get_delivery_point_by_tracking_token(normalized_guide)
    except SQLAlchemyError as error:
        _LOGGER.warning(
