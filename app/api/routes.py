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
    dispatcher = claims["role"] == "dispatcher"
    if not dispatcher and _claim_courier_id(claims) is None:
        return _error("La sesión de repartidor no es válida.", 403)
    parsed_id = _parse_uuid(route_id)
    if parsed_id is None:
        return _error("La ruta no existe.", 404)
    parsed_body = await _read_optional_model(request, RecalculateRouteRequest)
    if isinstance(parsed_body, JSONResponse):
        return parsed_body
    try:
        route = await RouteRepository(session).load_route(parsed_id)
        if route is None or (not dispatcher and route.courier_id != _claim_courier_id(claims)):
            return _error("La ruta no existe.", 404)
        if not dispatcher and parsed_body.delivery_point_ids:
            return _error("El repartidor solo puede optimizar sus paradas ya asignadas.", 403)
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
            route.courier_id,
            combined_ids,
            matrix_provider=cast(MatrixProvider, matrix_provider),
            route_provider=cast(RouteProvider, route_provider),
            traffic_ai_advisor=advisor,
            traffic_provider=traffic,
            allow_on_route_courier=True,
            existing_route_id=parsed_id,
        )
        result = await recalculate_route(session, parsed_id, plan, previous_ids)
        body = _operation_json(result, dispatcher=dispatcher)
        body["recalculated"] = True
        route_payload = body.get("route")
        if dispatcher and isinstance(route_payload, dict):
            route_payload["canUndo"] = True
        return _json(body, headers=_NO_STORE)
    except RoutePlanningError as error:
        return _error(str(error), error.status_code)
    except IntegrityError as error:
        await session.rollback()
        _log_database_error("Conflicto al recalcular la ruta", error)
        return _error("Un pedido cambió mientras se recalculaba la ruta.", 409)
    except (RoutingClientError, TimeoutError) as error:
        _LOGGER.warning(
            "No se pudo obtener la ruta vial",
            extra={"error_type": type(error).__name__},
        )
        return _error(
            "El proveedor vial no pudo calcular la matriz o el trazado. "
            "La ruta no se guardó sin calles verificadas.",
            502,
        )
    except SQLAlchemyError as error:
        await session.rollback()
        _log_database_error("No se pudo recalcular la ruta", error)
        return _error("No se pudo recalcular la ruta.", 500)


@router.post("/{route_id}/deshacer-recalculo")
async def undo_route_recalculation_endpoint(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)
    parsed_id = _parse_uuid(route_id)
    if parsed_id is None:
        return _error("La ruta no existe.", 404)
    try:
        result = await undo_route_recalculation(session, parsed_id)
        return _json(
            {
                "route": _route_json(result.route, dispatcher=True, can_undo=result.can_undo),
                "undone": True,
                "canUndo": result.can_undo,
                "releasedDeliveryPointIds": [
                    str(point_id) for point_id in result.released_delivery_point_ids
                ],
            },
            headers=_NO_STORE,
        )
    except RoutePlanningError as error:
        return _error(str(error), error.status_code)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_database_error("No se pudo restaurar el recálculo de la ruta", error)
        return _error("No se pudo restaurar el recálculo de la ruta.", 500)


@router.get("/{route_id}/navegacion")
async def get_route_navigation(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
) -> JSONResponse:
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    route_uuid, _dispatcher = authorized
    try:
        route = await RouteRepository(session).load_route(route_uuid)
        if route is None:
            return _error("La ruta no existe.", 404)
        if (
            claims is not None
            and claims["role"] == "courier"
            and route.courier_id != _claim_courier_id(claims)
        ):
            return _error("La ruta no existe.", 404)
        if route.status not in {RouteStatus.PLANNED, RouteStatus.IN_PROGRESS}:
            return _error("La ruta no está disponible para navegación.", 409)

        courier = route.courier
        if courier.current_lat is None or courier.current_lng is None:
            return _error("El repartidor todavía no tiene una ubicación actual.", 409)
        if not is_courier_location_fresh(courier.last_location_at):
            return _error(
                "La ubicación GPS está desactualizada. Comparte una posición reciente "
                "para actualizar el trazado.",
                409,
            )
        if not is_within_pasto_service_area(courier.current_lat, courier.current_lng):
            return _error("La última ubicación está fuera de Pasto.", 409)

        remaining = sorted(
            (
                point
                for point in route.delivery_points
                if point.status.value in {"PENDING", "EN_ROUTE"}
            ),
            key=lambda point: point.sequence_index if point.sequence_index is not None else 0,
        )
        if not remaining:
            return _json({"navigation": None})
        coordinates: list[Coordinate] = [(courier.current_lng, courier.current_lat)]
        coordinates.extend((point.lng, point.lat) for point in remaining)
        _matrix_provider, route_provider = providers
        result = await cast(RouteProvider, route_provider).get_route(coordinates)
        if result["geometry_provider"] not in {"OSRM", "ORS"}:
            return _error(
                "El proveedor no verificó una geometría vial; no se mostrará una línea recta.",
                503,
            )
        return _json(
            {
                "navigation": {
                    "geometry": result["geometry"],
                    "geometryProvider": result["geometry_provider"],
                    "distanceMeters": result["distance_meters"],
                    "durationMinutes": result["duration_minutes"],
                    "source": result["geometry_provider"].lower(),
                    "nextDeliveryPointId": str(remaining[0].id),
                    "remainingDeliveryPointIds": [str(point.id) for point in remaining],
                }
            },
            headers=_NO_STORE,
        )
    except RoutingClientError as error:
        _LOGGER.warning(
            "No se pudo actualizar la navegación vial",
            extra={"error_type": type(error).__name__},
        )
        return _error(
            "No se pudo obtener una ruta por calles. "
            "La navegación no se reemplazó por una línea recta.",
            503,
        )
    except SQLAlchemyError as error:
        _log_database_error("No se pudo consultar la navegación", error)
        return _error("No se pudo actualizar la navegación de la ruta.", 500)


@router.get("/{route_id}/eta")
async def get_route_eta(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
) -> JSONResponse:
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    route_uuid, _dispatcher = authorized
    try:
        route = await RouteRepository(session).load_route(route_uuid)
        if route is None:
            return _error("La ruta no existe.", 404)
        if (
            claims is not None
            and claims["role"] == "courier"
            and route.courier_id != _claim_courier_id(claims)
        ):
            return _error("La ruta no existe.", 404)

        points = sorted(
            (
                point
                for point in route.delivery_points
                if point.status.value in {"PENDING", "EN_ROUTE"}
            ),
            key=lambda point: point.sequence_index if point.sequence_index is not None else 0,
        )
        estimated_at = _date_json(datetime.now(UTC))
        if not points:
            return _json(
                {
                    "approximate": False,
                    "estimatedAt": estimated_at,
                    "etas": [],
                    "matrixSource": None,
                    "routeId": str(route.id),
                    "warning": "No hay paradas pendientes para estimar.",
                }
            )

        courier = route.courier
        if (
            courier.current_lat is None
            or courier.current_lng is None
            or not is_courier_location_fresh(courier.last_location_at)
            or not is_within_pasto_service_area(courier.current_lat, courier.current_lng)
        ):
            return _error(
                "El courier no tiene una posición reciente dentro de Pasto para calcular el ETA.",
                409,
            )

        coordinates: list[Coordinate] = [(courier.current_lng, courier.current_lat)]
        coordinates.extend((point.lng, point.lat) for point in points)
        matrix_provider, _route_provider = providers
        matrix_result = await cast(MatrixProvider, matrix_provider).get_duration_matrix(coordinates)
        etas = estimate_stop_etas_from_matrix(
            [str(point.id) for point in points],
            matrix_result["durations_minutes"],
            matrix_result.get("distances_meters"),
        )
        warnings = [
            matrix_result.get("warning"),
            "Los tiempos son estimaciones de la red vial; no incluyen tráfico en vivo.",
        ]
        if any(eta.estimated_minutes_from_now is None for eta in etas):
            warnings.append("No se encontró conexión vial para una o más paradas de la secuencia.")
        return _json(
            {
                "approximate": True,
                "estimatedAt": estimated_at,
                "etas": [
                    {
                        "stopId": eta.stop_id,
                        "distanceFromCourierMeters": eta.distance_from_courier_meters,
                        "estimatedMinutesFromNow": eta.estimated_minutes_from_now,
                    }
                    for eta in etas
                ],
                "matrixSource": matrix_result["source"],
                "routeId": str(route.id),
                "warning": " ".join(item for item in warnings if item),
            }
        )
    except RoutingClientError as error:
        _LOGGER.warning(
            "No se pudo calcular la matriz del ETA",
            extra={"error_type": type(error).__name__},
        )
        return _error(
            "El ETA vial no está disponible temporalmente; no se estimó en línea recta.", 503
        )
    except ValueError as error:
        _LOGGER.warning(
            "La matriz del ETA no cumple el contrato",
            extra={"error_type": type(error).__name__},
        )
        return _error("El ETA vial no está disponible temporalmente.", 503)
    except SQLAlchemyError as error:
        _log_database_error("No se pudo calcular el ETA de la ruta", error)
        return _error("No se pudo calcular el ETA de la ruta.", 500)


@router.get("/{route_id}/trafico")
async def get_route_traffic_profile(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    route_uuid, _dispatcher = authorized
    try:
        repository = RouteRepository(session)
        route = await repository.get_route_record(route_uuid)
        if route is None:
            return _error("La ruta no existe.", 404)
        if (
            claims is not None
            and claims["role"] == "courier"
            and route.courier_id != _claim_courier_id(claims)
        ):
            return _error("La ruta no existe.", 404)
        if route.started_at is None:
            return _error(
                "La ruta no tiene un inicio registrado; no se puede aislar su historial GPS.",
                409,
            )
        geometry = _read_route_geometry(route.geometry)
        if geometry is None:
            return _error(
                "La ruta no tiene geometría vial para construir segmentos.",
                409,
            )
        segments = derive_road_segments(geometry, route.estimated_duration_minutes)
        route_end = route.completed_at or datetime.now(UTC).replace(tzinfo=None)
        event_rows = await repository.list_location_events(
            courier_id=route.courier_id,
            start_at=route.started_at,
            end_at=route_end,
        )
        samples = match_location_events_to_segments(
            [LocationEventPoint((event.lng, event.lat), event.recorded_at) for event in event_rows],
            segments,
        )
        factors = calculate_congestion_factors(samples)
        return _json(
            {
                "factors": [{"factor": factor, "key": key} for key, factor in factors.items()],
                "routeId": str(route_uuid),
                "eventCount": len(event_rows),
                "sampleCount": len(samples),
                "segmentCount": len(segments),
                "warning": (
                    "El perfil solo usa GPS de esta ruta; generalizarlo a toda la ciudad "
                    "requiere un catálogo vial persistido."
                ),
            },
            headers=_NO_STORE,
        )
    except (ValueError, SQLAlchemyError) as error:
        if isinstance(error, SQLAlchemyError):
            _log_database_error("No se pudo leer el perfil de tráfico", error)
            return _error("No se pudo calcular el perfil histórico de tráfico.", 500)
        _LOGGER.warning(
            "La geometría de ruta no cumple el contrato de tráfico",
            extra={"error_type": type(error).__name__},
        )
        return _error("No se pudo calcular el perfil histórico de tráfico.", 409)


@router.get("/{route_id}/trafico-vivo")
async def get_route_live_traffic(
    route_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    traffic: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> JSONResponse:
    """Devuelve muestras TomTom recientes solo sobre la geometría vial de la ruta."""
    authorized = _authorize_route_id(route_id, claims)
    if isinstance(authorized, JSONResponse):
        return authorized
    route_uuid, _dispatcher = authorized
    try:
        route = await RouteRepository(session).load_route(route_uuid)
        if route is None:
            return _error("La ruta no existe.", 404)
        if (
            claims is not None
            and claims["role"] == "courier"
            and route.courier_id != _claim_courier_id(claims)
        ):
            return _error("La ruta no existe.", 404)
        if route.status not in {RouteStatus.PLANNED, RouteStatus.IN_PROGRESS}:
            return _error("El tráfico en vivo solo está disponible para rutas activas.", 409)
        geometry = _read_route_geometry(route.geometry)
        if (
            geometry is None
            or route.geometry_provider is None
            or any(
                not is_within_pasto_service_area(lat, lon)
                for lon, lat in geometry["coordinates"]
            )
        ):
            return _error("La ruta no tiene geometría vial válida dentro de Pasto.", 409)
        overlay = await sample_route_traffic_overlay(geometry, traffic)
        return _json(overlay, headers=_NO_STORE)
    except SQLAlchemyError as error:
        _log_database_error("No se pudo cargar el tráfico de la ruta", error)
        return _error("No se pudo cargar el tráfico de la ruta.", 500)


def _route_json(
    route: Route,
    *,
    dispatcher: bool,
    can_undo: bool = False,
) -> dict[str, object]:
    now = datetime.now(UTC).replace(tzinfo=None)
    courier = route.courier
    fresh = (
        courier.current_lat is not None
        and courier.current_lng is not None
        and is_courier_location_fresh(courier.last_location_at, now)
        and is_within_pasto_service_area(courier.current_lat, courier.current_lng)
    )
    ordered_points = sorted(
        route.delivery_points,
        key=lambda point: (
            point.sequence_index is None,
            point.sequence_index if point.sequence_index is not None else 0,
        ),
    )
    courier_json: dict[str, object] = {
        "currentLat": courier.current_lat if fresh else None,
        "currentLng": courier.current_lng if fresh else None,
    }
    if dispatcher:
        courier_json.update(
            {
                "id": str(courier.id),
                "name": courier.name,
                "phone": courier.phone,
                "status": courier.status.value,
                "lastLocationAt": _date_json(courier.last_location_at),
            }
        )
    route_data: dict[str, object] = {
        "id": str(route.id),
        "status": route.status.value,
        "estimatedDurationMinutes": route.estimated_duration_minutes,
        "estimatedDistanceMeters": route.estimated_distance_meters,
        "baselineDurationMinutes": route.baseline_duration_minutes,
        "baselineDistanceMeters": route.baseline_distance_meters,
        "geometry": route.geometry,
        "geometryProvider": route.geometry_provider.value if route.geometry_provider else None,
        "startedAt": _date_json(route.started_at),
        "completedAt": _date_json(route.completed_at),
        "optimizedAt": _date_json(route.optimized_at or route.created_at),
        "createdAt": _date_json(route.created_at),
        "updatedAt": _date_json(route.updated_at),
        "courier": courier_json,
        "deliveryPoints": [_point_json(point, dispatcher=dispatcher) for point in ordered_points],
    }
    if dispatcher:
        route_data["courierId"] = str(route.courier_id)
        route_data["canUndo"] = can_undo
    return route_data


def _point_json(point: DeliveryPoint, *, dispatcher: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(point.id),
        "address": point.address,
        "lat": point.lat,
        "lng": point.lng,
        "timeWindow": point.time_window,
        "status": point.status.value,
        "sequenceIndex": point.sequence_index,
    }
    if dispatcher:
        payload.update(
            {
                "orderId": point.order_id,
                "trackingToken": point.tracking_token,
                "routeId": str(point.route_id) if point.route_id else None,
            }
        )
    return payload


def _read_route_geometry(value: object) -> GeoJsonLineString | None:
    if not isinstance(value, dict) or value.get("type") != "LineString":
        return None
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list):
        return None
    parsed: list[Coordinate] = []
    for coordinate in coordinates:
        if (
            not isinstance(coordinate, (list, tuple))
            or len(coordinate) != 2
            or any(
                isinstance(part, bool) or not isinstance(part, (int, float)) for part in coordinate
            )
        ):
            return None
        parsed.append((float(coordinate[0]), float(coordinate[1])))
    return {"type": "LineString", "coordinates": parsed}


def _operation_json(
    result: RouteOperationResult, *, dispatcher: bool = True
) -> dict[str, object]:
    route = result.route
    point_ids = result.ordered_delivery_point_ids
    return {
        "matrixSource": result.matrix_source,
        "orderedDeliveryPointIds": [str(point_id) for point_id in point_ids],
        "trafficAi": result.traffic_ai,
        "liveTraffic": result.live_traffic,
        "route": _route_json(route, dispatcher=dispatcher),
        **({"warning": result.warning} if result.warning is not None else {}),
    }


def _authorize_route_id(
    route_id: str,
    claims: SessionClaims | None,
) -> tuple[UUID, bool] | JSONResponse:
    parsed = _parse_uuid(route_id)
    if parsed is None:
        return _error("La ruta no existe.", 404)
    if claims is None:
        return _error("Debes iniciar sesión para consultar rutas.", 401)
    if claims["role"] == "courier" and _claim_courier_id(claims) is None:
        return _error("La sesión de repartidor no es válida.", 403)
    return parsed, claims["role"] == "dispatcher"


def _claim_courier_id(claims: SessionClaims) -> UUID | None:
    try:
        value = claims.get("courierId")
        return UUID(value) if value else None
    except ValueError:
        return None


def _parse_query_courier_id(raw: str | None) -> UUID | None | JSONResponse:
    if raw is None:
        return None
    parsed = _parse_uuid(raw)
    return parsed if parsed is not None else _error("courierId debe ser un UUID válido.", 400)


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


async def _read_model[T: BaseModel](request: Request, model: type[T]) -> T | JSONResponse:
    try:
        raw: object = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("El body debe ser JSON válido.", 400)
    if not isinstance(raw, dict):
        return _error("El body debe ser un objeto JSON.", 400)
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        return _validation_error(error)


async def _read_optional_model[T: BaseModel](
    request: Request,
    model: type[T],
) -> T | JSONResponse:
    raw_body = await request.body()
    if not raw_body.strip():
        return model.model_validate({})
    try:
        raw: object = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("El body debe ser JSON válido.", 400)
    if not isinstance(raw, dict):
        return _error("El body debe ser un objeto JSON.", 400)
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        return _validation_error(error)


def _validation_error(error: ValidationError) -> JSONResponse:
    first = error.errors()[0]
    field = str(first.get("loc", ["body"])[-1])
    if field == "courier_id":
        message = "courierId debe ser un UUID válido."
    elif field == "delivery_point_ids":
        message = f"deliveryPointIds debe contener entre 1 y {MAX_ROUTE_STOPS} UUID válidos."
    elif field == "status":
        message = "status debe ser PLANNED, IN_PROGRESS, COMPLETED o CANCELLED."
    else:
        message = "El body de la solicitud no es válido."
    return _error(message, 400)


def _date_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log_database_error(message: str, error: Exception) -> None:
    _LOGGER.warning(message, extra={"error_type": type(error).__name__})


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": message},
        status_code=status_code,
        headers=_NO_STORE,
    )


def _json(
    content: dict[str, object],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers=headers or _NO_STORE,
    )
