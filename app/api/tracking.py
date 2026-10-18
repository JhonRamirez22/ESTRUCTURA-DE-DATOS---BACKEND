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
from app.services.customer_eta_traffic import (
    FirstLegTrafficEstimate,
    estimate_first_leg_with_traffic,
)
from app.services.route_planner import get_routing_providers
from app.services.traffic import TrafficService, get_traffic_service

router = APIRouter(prefix="/seguimiento", tags=["seguimiento"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store, private"}
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

    matrix_provider, route_provider = providers
    try:
        delivery_point = await DeliveryPointRepository(
            session
        ).get_delivery_point_by_tracking_token(normalized_guide)
    except SQLAlchemyError as error:
        _LOGGER.warning(
            "No se pudo consultar el seguimiento en la base de datos",
            extra={"error_type": type(error).__name__},
        )
        return _error(
            "El seguimiento no está disponible temporalmente. Intenta de nuevo más tarde.",
            503,
        )

    if delivery_point is None:
        return _error("No encontramos un pedido con esa guía.", 404)

    base_tracking: dict[str, object] = {
        "guide": delivery_point.tracking_token,
        "status": delivery_point.status.value,
        "address": delivery_point.address,
        "coordinates": {"lat": delivery_point.lat, "lng": delivery_point.lng},
    }
    route = delivery_point.route
    if route is None:
        return _json({"tracking": {**base_tracking, "route": None}})

    ordered_points = sorted(
        route.delivery_points,
        key=lambda point: (
            point.sequence_index if point.sequence_index is not None else 2**31
        ),
    )
    remaining_points = [
        point for point in ordered_points if point.status not in _TERMINAL_POINT_STATUSES
    ]
    now = datetime.now(UTC).replace(tzinfo=None)
    courier = route.courier
    precise_position = None
    if (
        courier.current_lat is not None
        and courier.current_lng is not None
        and is_courier_location_fresh(courier.last_location_at, now)
        and is_within_pasto_service_area(courier.current_lat, courier.current_lng)
    ):
        precise_position = (courier.current_lat, courier.current_lng)

    is_next_stop = bool(remaining_points) and remaining_points[0].id == delivery_point.id
    target_is_active = any(point.id == delivery_point.id for point in remaining_points)
    route_is_active = route.status in _ACTIVE_ROUTE_STATUSES
    may_show_courier = target_is_active and route_is_active
    shared_position = (
        (_round_coordinate(precise_position[0]), _round_coordinate(precise_position[1]))
        if precise_position is not None and may_show_courier
        else None
    )

    matrix_result: MatrixProviderResult | None = None
    stop_etas: list[StopEta] = []
    estimate_warning: str | None = None
    if shared_position is not None and remaining_points:
        try:
            matrix_result = await matrix_provider.get_duration_matrix(
                [
                    (shared_position[1], shared_position[0]),
                    *((point.lng, point.lat) for point in remaining_points),
                ]
            )
        except Exception as error:
            _LOGGER.warning(
                "No se pudo calcular el ETA vial del seguimiento",
                extra={"error_type": type(error).__name__},
            )
            estimate_warning = (
                "El ETA por calles no está disponible ahora; "
                "no se estimó con distancias en línea recta."
            )

    geometry: GeoJsonLineString | None = None
    geometry_provider: str | None = None
    road_distance_meters: float | None = None
    road_duration_minutes: float | None = None
    road_route: RouteResult | None = None
    navigation_warning: str | None = None
    if shared_position is not None:
        # La guía solo autoriza compartir el tramo hacia su propio pedido.
        try:
            road_route = await route_provider.get_route(
                [
                    (shared_position[1], shared_position[0]),
                    (delivery_point.lng, delivery_point.lat),
                ]
            )
            if road_route["geometry_provider"] in {"OSRM", "ORS"}:
                geometry = road_route["geometry"]
                geometry_provider = road_route["geometry_provider"]
                road_distance_meters = road_route["distance_meters"]
                road_duration_minutes = road_route["duration_minutes"]
            else:
                road_route = None
                navigation_warning = (
                    "El proveedor no verificó el trazado por calles. "
