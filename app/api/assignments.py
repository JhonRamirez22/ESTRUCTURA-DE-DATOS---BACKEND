"""Propuestas de asignación y aplicación transaccional a rutas."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse

from app.api.auth import get_optional_session
from app.core.auth import SessionClaims
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.optimization.assign_orders import (
    AssignOrdersOptions,
    Coordinates,
    CourierPosition,
    OrderPoint,
    assign_orders,
)
from app.core.optimization.joint_assignment import JointAssignmentResult, improve_assignment
from app.core.routing.cached_matrix_provider import CachedMatrixProvider, MatrixProviderResult
from app.core.routing.cached_route_provider import CachedRouteProvider
from app.core.routing.routing_errors import RoutingClientError
from app.db.session import get_session, get_session_factory
from app.models import CustomerNotificationChannel
from app.repositories.courier_repository import CourierRepository
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.services.customer_notifications import (
    any_customer_notification_delivery_configured,
    customer_notification_channels_configured,
    dispatch_pending_customer_notifications_background,
)
from app.services.delivery_events import EventActor
from app.services.route_planner import (
    MAX_ROUTE_STOPS,
    MatrixProvider,
    OptimizedRoutePlan,
    RouteOperationResult,
    RoutePlanningError,
    RouteProvider,
    build_optimized_route_plan,
    create_optimized_routes_batch,
    get_route_optimization,
    get_routing_providers,
    load_plan_inputs,
)
from app.services.route_traffic_advisor import (
    HttpRouteTrafficAdvisor,
    get_route_traffic_advisor,
)
from app.services.traffic import TrafficService, get_traffic_service
from app.settings import get_settings

router = APIRouter(prefix="/asignaciones", tags=["asignaciones"])
MAX_UNROUTABLE_DROPS = 5
_LOGGER = logging.getLogger(__name__)
# Con el servidor vial público, más de 3 consultas simultáneas suelen toparse con su límite.
MAX_CONCURRENT_PLANS = 3
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}


class AssignmentProposalRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    courier_ids: list[UUID] | None = Field(default=None, validation_alias="courierIds")
    delivery_point_ids: list[UUID] | None = Field(
        default=None,
        validation_alias="deliveryPointIds",
    )

    @model_validator(mode="after")
    def ensure_unique_ids(self) -> AssignmentProposalRequest:
        if self.courier_ids is not None and len(set(self.courier_ids)) != len(self.courier_ids):
            raise ValueError("courierIds no puede contener duplicados.")
        if (
            self.delivery_point_ids is not None
            and len(set(self.delivery_point_ids)) != len(self.delivery_point_ids)
        ):
            raise ValueError("deliveryPointIds no puede contener duplicados.")
        return self


class AssignmentItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    courier_id: UUID = Field(validation_alias="courierId")
    delivery_point_ids: list[UUID] = Field(
        min_length=1,
        max_length=MAX_ROUTE_STOPS,
        validation_alias="deliveryPointIds",
    )

    @model_validator(mode="after")
    def ensure_unique_stops(self) -> AssignmentItem:
        if len(set(self.delivery_point_ids)) != len(self.delivery_point_ids):
            raise ValueError("deliveryPointIds no puede contener duplicados.")
        return self


class ApplyAssignmentsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    assignments: list[AssignmentItem] = Field(min_length=1)

    @model_validator(mode="after")
    def ensure_unique_targets(self) -> ApplyAssignmentsRequest:
        courier_ids = [item.courier_id for item in self.assignments]
        point_ids = [point_id for item in self.assignments for point_id in item.delivery_point_ids]
        if len(set(courier_ids)) != len(courier_ids):
            raise ValueError("Cada courier solo puede aparecer una vez en assignments.")
        if len(set(point_ids)) != len(point_ids):
            raise ValueError("Un delivery point no puede asignarse a más de un courier.")
        return self


@router.post("")
async def propose_assignments(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)
    body = await _read_object(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        data = AssignmentProposalRequest.model_validate(body)
    except ValidationError as error:
        return _validation_error(error)

    now = datetime.now(UTC).replace(tzinfo=None)
    fresh_after = now - timedelta(minutes=2)
    try:
        couriers = await CourierRepository(session).list_available_for_assignment(
            fresh_after=fresh_after,
            as_of=now,
            courier_ids=data.courier_ids,
        )
        raw_points = await DeliveryPointRepository(session).list_pending_unassigned(
            data.delivery_point_ids
        )
        points = [
            point
            for point in raw_points
            if is_within_pasto_service_area(point.lat, point.lng)
        ]
        couriers = [
            courier
            for courier in couriers
            if courier.current_lat is not None
            and courier.current_lng is not None
            and is_within_pasto_service_area(courier.current_lat, courier.current_lng)
        ]
    except SQLAlchemyError as error:
        _log_database_error("No se pudo consultar datos para asignación", error)
        return _error("No se pudo calcular la asignación de pedidos.", 500)

    candidate_count = 0
    matrix_source = "not-needed"
    travel_costs_minutes: dict[tuple[str, str], float | None] = {}
    road_durations: list[list[float | None]] | None = None
    if couriers and points:
        matrix_provider, _ = providers
        matrix_result: MatrixProviderResult | None = None
        for _attempt in range(MAX_UNROUTABLE_DROPS + 1):
            courier_coordinates = [
                (courier.current_lng, courier.current_lat)
                for courier in couriers
                if courier.current_lat is not None and courier.current_lng is not None
            ]
            point_coordinates = [(point.lng, point.lat) for point in points]
            coordinates = [*courier_coordinates, *point_coordinates]
            if not courier_coordinates or not point_coordinates:
                break
            try:
                matrix_result = await matrix_provider.get_duration_matrix(coordinates)
                break
            except RoutingClientError as error:
                bad = _unroutable_index(error)
                if bad is None or not 0 <= bad < len(coordinates):
                    _LOGGER.warning(
                        "No se pudo obtener la matriz vial para asignar pedidos",
                        extra={"error_type": type(error).__name__,
                               "status": getattr(error, "status", None)},
                    )
                    return _error(
                        "No se pudo consultar la red vial para asignar los pedidos. "
                        "Inténtalo de nuevo.",
                        502,
                    )
                # Un punto sin calle cercana (a más de 50 m) no debe tumbar todo el análisis:
                # se deja fuera y aparece como «sin asignar».
                if bad < len(courier_coordinates):
                    del couriers[bad]
                else:
                    del points[bad - len(courier_coordinates)]
        if matrix_result is None:
            couriers, points = [], []
        else:
            matrix_source = matrix_result.get("source", "osrm")
            durations = matrix_result["durations_minutes"]
            distances = matrix_result.get("distances_meters")
            matrix_size = len(coordinates)
            if len(durations) != matrix_size or any(len(row) != matrix_size for row in durations):
                return _error("El proveedor vial devolvió una matriz de asignación inválida.", 502)
            if distances is not None and (
                len(distances) != matrix_size
                or any(len(row) != matrix_size for row in distances)
            ):
                return _error("El proveedor vial devolvió distancias inválidas.", 502)

            point_offset = len(courier_coordinates)
            road_durations = durations
            for courier_index, courier in enumerate(couriers):
                for point_index, point in enumerate(points):
                    pair = (str(courier.id), str(point.id))
                    destination_index = point_offset + point_index
                    duration = durations[courier_index][destination_index]
                    travel_costs_minutes[pair] = duration
                    if duration is not None:
                        candidate_count += 1
