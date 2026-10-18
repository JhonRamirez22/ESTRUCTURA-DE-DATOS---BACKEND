"""API de creación, consulta, recálculo y ciclo de vida de rutas."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import get_optional_session
from app.core.auth import SessionClaims
from app.core.location.courier_location import is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.metrics.estimate_eta import estimate_stop_etas_from_matrix
from app.core.routing.cached_matrix_provider import CachedMatrixProvider
from app.core.routing.cached_route_provider import CachedRouteProvider
from app.core.routing.contracts import Coordinate, GeoJsonLineString
from app.core.routing.routing_errors import RoutingClientError
from app.core.traffic.congestion_factors import calculate_congestion_factors
from app.core.traffic.map_matching import LocationEventPoint, match_location_events_to_segments
from app.core.traffic.road_segments import derive_road_segments
from app.db.session import get_session
from app.models import (
    DeliveryPoint,
    Route,
    RouteStatus,
)
from app.repositories.route_repository import RouteRepository
from app.services.route_planner import (
    MAX_ROUTE_STOPS,
    MatrixProvider,
    RouteOperationResult,
    RoutePlanningError,
    RouteProvider,
    build_optimized_route_plan,
    create_optimized_route,
    get_routing_providers,
    recalculate_route,
    undo_route_recalculation,
    update_route_status,
)
from app.services.route_traffic_advisor import (
    RouteTrafficAdvisor,
    get_route_traffic_advisor,
)
from app.services.route_traffic_overlay import sample_route_traffic_overlay
from app.services.traffic import TrafficService, get_traffic_service

router = APIRouter(prefix="/rutas", tags=["rutas"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}


class CreateRouteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", str_strip_whitespace=True)

    courier_id: UUID = Field(validation_alias="courierId")
    delivery_point_ids: list[UUID] = Field(
        min_length=1,
        max_length=MAX_ROUTE_STOPS,
        validation_alias="deliveryPointIds",
    )


class RecalculateRouteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    delivery_point_ids: list[UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ROUTE_STOPS,
        validation_alias="deliveryPointIds",
    )


class RouteStatusRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: RouteStatus


@router.get("")
async def list_routes(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para consultar rutas.", 401)
    if claims["role"] == "courier":
        courier_filter = _claim_courier_id(claims)
        if courier_filter is None:
            return _error("La sesión de repartidor no es válida.", 403)
    else:
        parsed_courier_filter = _parse_query_courier_id(request.query_params.get("courierId"))
        if isinstance(parsed_courier_filter, JSONResponse):
            return parsed_courier_filter
        courier_filter = parsed_courier_filter

    status: RouteStatus | None = None
    raw_status = request.query_params.get("status")
    if raw_status is not None:
        try:
            status = RouteStatus(raw_status)
        except ValueError:
            return _error("status debe ser PLANNED, IN_PROGRESS, COMPLETED o CANCELLED.", 400)
    try:
        routes = await RouteRepository(session).list_routes(
            courier_id=courier_filter,
            status=status,
        )
        dispatcher = claims["role"] == "dispatcher"
        undoable_route_ids = (
            await RouteRepository(session).route_ids_with_revisions([route.id for route in routes])
            if dispatcher
            else set()
        )
        return _json(
            {
                "routes": [
                    _route_json(
                        route,
                        dispatcher=dispatcher,
                        can_undo=route.id in undoable_route_ids,
                    )
                    for route in routes
                ]
            },
            headers=_NO_STORE,
        )
    except SQLAlchemyError as error:
        _log_database_error("No se pudieron consultar las rutas", error)
        return _error("No se pudieron consultar las rutas.", 500)


@router.post("", status_code=201)
async def create_route(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
    advisor: Annotated[RouteTrafficAdvisor | None, Depends(get_route_traffic_advisor)],
    traffic: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)
    parsed = await _read_model(request, CreateRouteRequest)
    if isinstance(parsed, JSONResponse):
        return parsed
    if len(set(parsed.delivery_point_ids)) != len(parsed.delivery_point_ids):
        return _error("deliveryPointIds no puede contener duplicados.", 400)
    matrix_provider, route_provider = providers
    try:
        plan = await build_optimized_route_plan(
            session,
            parsed.courier_id,
            parsed.delivery_point_ids,
            matrix_provider=cast(MatrixProvider, matrix_provider),
            route_provider=cast(RouteProvider, route_provider),
            traffic_ai_advisor=advisor,
            traffic_provider=traffic,
        )
        result = await create_optimized_route(session, plan)
        return _json(_operation_json(result), status_code=201, headers=_NO_STORE)
    except RoutePlanningError as error:
        return _error(str(error), error.status_code)
    except IntegrityError as error:
        await session.rollback()
        _log_database_error("Conflicto al guardar la ruta", error)
        return _error("Un pedido o el repartidor cambió mientras se creaba la ruta.", 409)
    except (RoutingClientError, TimeoutError) as error:
        _LOGGER.warning(
            "No se pudo obtener una ruta vial",
            extra={"error_type": type(error).__name__},
        )
        return _error(
            "El proveedor vial no pudo calcular la matriz o el trazado. "
            "La ruta no se guardó sin calles verificadas.",
            502,
        )
    except SQLAlchemyError as error:
        await session.rollback()
        _log_database_error("No se pudo guardar la ruta", error)
        return _error("No se pudo crear la ruta.", 500)


@router.get("/{route_id}")
async def get_route(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    parsed_id, dispatcher = authorized
    try:
        route = await RouteRepository(session).load_route(parsed_id)
        if route is None:
            return _error("La ruta no existe.", 404)
        if (
            claims is not None
            and claims["role"] == "courier"
            and route.courier_id != _claim_courier_id(claims)
        ):
            return _error("La ruta no existe.", 404)
        can_undo = bool(
            dispatcher
            and await RouteRepository(session).route_ids_with_revisions([route.id])
        )
        return _json(
            {"route": _route_json(route, dispatcher=dispatcher, can_undo=can_undo)},
            headers=_NO_STORE,
        )
    except SQLAlchemyError as error:
        _log_database_error("No se pudo consultar la ruta", error)
        return _error("No se pudieron consultar las rutas.", 500)


@router.patch("/{route_id}")
async def patch_route_status(
    route_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    parsed_id, dispatcher = authorized
    parsed_body = await _read_model(request, RouteStatusRequest)
    if isinstance(parsed_body, JSONResponse):
        return parsed_body
    try:
        existing_courier_id = await RouteRepository(session).get_route_courier_id(parsed_id)
        if existing_courier_id is None:
            return _error("La ruta no existe.", 404)
        if not dispatcher and existing_courier_id != _claim_courier_id(cast(SessionClaims, claims)):
            return _error("La ruta no existe.", 404)
        route = await update_route_status(session, parsed_id, parsed_body.status)
        return _json({"route": _route_json(route, dispatcher=dispatcher)}, headers=_NO_STORE)
    except RoutePlanningError as error:
        return _error(str(error), error.status_code)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_database_error("No se pudo actualizar el estado de ruta", error)
        return _error("No se pudo actualizar el estado de la ruta.", 500)


@router.post("/{route_id}/recalcular")
async def recalculate_route_endpoint(
    route_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
    advisor: Annotated[RouteTrafficAdvisor | None, Depends(get_route_traffic_advisor)],
    traffic: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)
    parsed_id = _parse_uuid(route_id)
    if parsed_id is None:
        return _error("La ruta no existe.", 404)
    parsed_body = await _read_optional_model(request, RecalculateRouteRequest)
    if isinstance(parsed_body, JSONResponse):
        return parsed_body
    try:
        route = await RouteRepository(session).load_route(parsed_id)
        if route is None:
            return _error("La ruta no existe.", 404)
        if route.status not in {RouteStatus.PLANNED, RouteStatus.IN_PROGRESS}:
            return _error("Solo se pueden recalcular rutas planificadas o en progreso.", 409)
        previous_ids = [
            point.id
            for point in sorted(route.delivery_points, key=lambda item: item.sequence_index or 0)
            if point.status.value in {"PENDING", "EN_ROUTE"}
        ]
        requested_ids = parsed_body.delivery_point_ids or []
        combined_ids = list(dict.fromkeys([*previous_ids, *requested_ids]))
        if not combined_ids:
            return _error("La ruta no tiene puntos pendientes para recalcular.", 409)
        matrix_provider, route_provider = providers
        plan = await build_optimized_route_plan(
            session,
