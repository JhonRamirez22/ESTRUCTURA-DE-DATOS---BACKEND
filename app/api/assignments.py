"""Propuestas de asignación y aplicación transaccional a rutas."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse

from app.api.auth import get_optional_session
from app.core.ai.assignment_traffic import (
    MAX_ASSIGNMENT_TRAFFIC_AI_CANDIDATES,
    AssignmentTrafficRequest,
    build_assignment_candidates,
    supported_assignment_adjustments,
)
from app.core.auth import SessionClaims
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.optimization.assign_orders import (
    AssignmentAdjustment,
    AssignOrdersOptions,
    Coordinates,
    CourierPosition,
    OrderPoint,
    assign_orders,
)
from app.core.routing.cached_matrix_provider import CachedMatrixProvider
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
from app.services.route_planner import (
    MAX_ROUTE_STOPS,
    MatrixProvider,
    OptimizedRoutePlan,
    RouteOperationResult,
    RoutePlanningError,
    RouteProvider,
    build_optimized_route_plan,
    create_optimized_routes_batch,
    get_routing_providers,
)
from app.services.route_traffic_advisor import (
    HttpRouteTrafficAdvisor,
    get_route_traffic_advisor,
)
from app.services.traffic import TrafficService, get_traffic_service

router = APIRouter(prefix="/asignaciones", tags=["asignaciones"])
_LOGGER = logging.getLogger(__name__)
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
    advisor: Annotated[
        HttpRouteTrafficAdvisor | None,
        Depends(get_route_traffic_advisor),
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

    potential_candidate_count = len(couriers) * len(points)
    candidate_count = 0
    matrix_source = "not-needed"
    travel_costs_minutes: dict[tuple[str, str], float | None] = {}
    road_metrics_by_id: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    has_verified_road_costs = False
    if couriers and points:
        matrix_provider, _ = providers
        courier_coordinates = [
            (courier.current_lng, courier.current_lat)
            for courier in couriers
            if courier.current_lat is not None and courier.current_lng is not None
        ]
        point_coordinates = [(point.lng, point.lat) for point in points]
        coordinates = [*courier_coordinates, *point_coordinates]
        try:
            matrix_result = await matrix_provider.get_duration_matrix(coordinates)
        except RoutingClientError as error:
            _LOGGER.warning(
                "No se pudo obtener la matriz vial para asignar pedidos",
                extra={"error_type": type(error).__name__},
            )
            return _error(
                "No se pudo consultar la red vial para asignar los pedidos. Inténtalo de nuevo.",
                502,
            )

        matrix_source = matrix_result.get("source", "osrm")
        has_verified_road_costs = matrix_source in {"osrm", "ors"}
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
        for courier_index, courier in enumerate(couriers):
            for point_index, point in enumerate(points):
                pair = (str(courier.id), str(point.id))
                destination_index = point_offset + point_index
                duration = durations[courier_index][destination_index]
                travel_costs_minutes[pair] = duration
                if duration is not None:
                    candidate_count += 1
                road_metrics_by_id[pair] = (
                    distances[courier_index][destination_index] if distances is not None else None,
                    duration,
                )

    ai_status: dict[str, object] = {
        "source": "deterministic",
        "applied": False,
        "routingSource": matrix_source,
        "warning": (
            "No hay repartidores o pedidos disponibles para calcular."
            if candidate_count == 0
            else "Groq no está disponible; se asignó con costos de la matriz vial."
            if has_verified_road_costs
            else "La red vial no pudo verificarse; la asignación es aproximada en línea recta."
        ),
    }
    adjustments: list[AssignmentAdjustment] = []
    if potential_candidate_count and not candidate_count:
        ai_status["warning"] = "No hay caminos viales disponibles entre repartidores y pedidos."
    elif not has_verified_road_costs and candidate_count:
        ai_status["warning"] = (
            "Groq se omitió porque no hay costos de calles verificados; "
            "se usó una aproximación en línea recta."
        )
    elif advisor is None and candidate_count:
        ai_status["warning"] = (
            "Groq no está configurado o la asesoría de ubicaciones está desactivada; "
            f"se asignó con tiempos viales de {matrix_source.upper()}."
        )
    elif candidate_count > MAX_ASSIGNMENT_TRAFFIC_AI_CANDIDATES:
        ai_status["warning"] = (
            f"Groq se omitió porque hay {candidate_count} pares candidatos "
            f"(máximo {MAX_ASSIGNMENT_TRAFFIC_AI_CANDIDATES}); se usaron tiempos viales."
        )
    elif advisor is not None and candidate_count:
        courier_aliases = {
            courier.id: f"courier-{index + 1}" for index, courier in enumerate(couriers)
        }
        order_aliases = {
            point.id: f"order-{index + 1}" for index, point in enumerate(points)
        }
        courier_aliases_by_id = {str(key): alias for key, alias in courier_aliases.items()}
        order_aliases_by_id = {str(key): alias for key, alias in order_aliases.items()}
        courier_ids_by_alias = {alias: str(key) for key, alias in courier_aliases.items()}
        order_ids_by_alias = {alias: str(key) for key, alias in order_aliases.items()}
        road_metrics_by_alias = {
            (courier_aliases_by_id[courier_id], order_aliases_by_id[order_id]): metrics
            for (courier_id, order_id), metrics in road_metrics_by_id.items()
        }
        candidates = build_assignment_candidates(
            [
                (courier_aliases[courier.id], (courier.current_lng, courier.current_lat))
                for courier in couriers
                if courier.current_lat is not None and courier.current_lng is not None
            ],
            [(order_aliases[point.id], (point.lng, point.lat)) for point in points],
            road_metrics=road_metrics_by_alias,
        )
        try:
            advice = await advisor.get_assignment_advice(
                AssignmentTrafficRequest(tuple(candidates), datetime.now(UTC))
            )
            supported, ignored = supported_assignment_adjustments(
                candidates, advice.adjustments
            )
            adjustments = [
                AssignmentAdjustment(
                    courier_ids_by_alias[item.courier_id],
                    order_ids_by_alias[item.order_id],
                    item.traffic_multiplier,
                )
                for item in supported
            ]
            ai_status = {
                "source": "external-ai",
                "applied": bool(adjustments),
                "model": advice.model,
                "routingSource": matrix_source,
                "warning": _ai_warning(len(adjustments), ignored),
            }
        except Exception as error:
            _LOGGER.warning(
                "Groq no produjo una asesoría válida; se conserva la matriz vial",
                extra={"error_type": type(error).__name__},
            )
            ai_status = {
                "source": "deterministic",
                "applied": False,
                "routingSource": matrix_source,
                "warning": (
                    f"Groq no respondió de forma utilizable; se asignó con tiempos viales "
                    f"de {matrix_source.upper()}."
                ),
            }

    result = assign_orders(
        [
            CourierPosition(
                str(courier.id),
                Coordinates(courier.current_lat, courier.current_lng),
                True,
            )
            for courier in couriers
            if courier.current_lat is not None and courier.current_lng is not None
        ],
        [OrderPoint(str(point.id), Coordinates(point.lat, point.lng)) for point in points],
        options=AssignOrdersOptions(adjustments, travel_costs_minutes),
    )
    assignments = [
        {"courierId": courier_id, "deliveryPointIds": point_ids}
        for courier_id, point_ids in result.items()
    ]
    assigned_ids = {
        point_id for point_ids in result.values() for point_id in point_ids
    }
    requested_ids = (
        [str(point_id) for point_id in data.delivery_point_ids]
        if data.delivery_point_ids is not None
        else [str(point.id) for point in raw_points]
    )
    return _json(
        {
            "assignments": assignments,
            "unassignedDeliveryPointIds": [
                point_id for point_id in requested_ids if point_id not in assigned_ids
            ],
            "ai": ai_status,
        }
    )


@router.post("/aplicar", status_code=201)
async def apply_assignments(
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
    advisor: Annotated[
        HttpRouteTrafficAdvisor | None,
        Depends(get_route_traffic_advisor),
    ],
    traffic: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)
    body = await _read_object(request)
