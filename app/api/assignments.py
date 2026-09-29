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
