"""CRUD de puntos de entrega; conserva contratos HTTP de /api/pedidos."""

from __future__ import annotations

import json
import logging
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import JsonValue, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, Response

from app.api.auth import get_optional_session
from app.core.auth import SessionClaims
from app.core.location.pasto_area import (
    contains_foreign_city_reference,
    is_within_pasto_service_area,
    normalize_pasto_address,
)
from app.db.session import get_session
from app.models import (
    DeliveryPoint,
    DeliveryPointStatus,
    Notification,
    NotificationKind,
    RouteStatus,
)
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.repositories.notification_repository import NotificationRepository
from app.schemas.orders import (
    DeliveryPointView,
    OrderCreateRequest,
    OrderUpdateRequest,
    RouteSummaryView,
)
from app.services.access_sessions import utc_now
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_normalization import AddressNormalizationResult, normalize_address
from app.services.customer_notifications import cancel_pending_customer_notifications
from app.services.delivery_events import DeliveryEventRecorder, EventActor
from app.services.scheduling import DeliverySchedule
from app.settings import get_settings

router = APIRouter(prefix="/pedidos", tags=["pedidos"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}


@router.get("")
async def list_delivery_points(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    access_error = _dispatcher_access_error(claims)
    if access_error is not None:
        return access_error

    status: DeliveryPointStatus | None = None
    raw_status = request.query_params.get("status")
    if raw_status is not None:
        try:
            status = DeliveryPointStatus(raw_status)
        except ValueError:
            return _error("status debe ser REQUESTED, PENDING, EN_ROUTE, DELIVERED o FAILED.", 400)

    try:
        points = await DeliveryPointRepository(session).list_delivery_points(status)
        return _json(
            {"deliveryPoints": [_point_json(point) for point in points]},
            headers=_NO_STORE,
        )
    except SQLAlchemyError as error:
        _log_database_error("No se pudo consultar la lista de pedidos", error)
        return _error("No se pudo procesar el pedido.", 500)


@router.post("", status_code=201)
async def create_delivery_point(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    advisor: Annotated[AddressAiAdvisor | None, Depends(get_address_ai_advisor)],
) -> JSONResponse:
    access_error = _dispatcher_access_error(claims)
    if access_error is not None:
        return access_error

    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        data = OrderCreateRequest.model_validate(body)
    except ValidationError as error:
        return _validation_error(error)

    address = normalize_pasto_address(data.address)
    invalid_location = _validate_pasto_location(address, data.lat, data.lng)
    if invalid_location:
        return _error(invalid_location, 400)

    normalization = await normalize_address(address, data.lat, data.lng, advisor)
    point = DeliveryPoint(
        id=uuid4(),
        address=normalization.address,
        lat=data.lat,
        lng=data.lng,
        time_window=data.time_window,
        priority=data.priority,
        scheduled_for=data.scheduled_for,
        status=DeliveryPointStatus.PENDING,
        order_id=data.order_id,
        customer_email=data.customer_email,
        email_notifications_enabled=data.email_notifications_enabled,
        customer_phone=data.customer_phone,
        sms_notifications_enabled=data.sms_notifications_enabled,
        tracking_token=str(uuid4()),
    )
    repository = DeliveryPointRepository(session)
    try:
        repository.add(point)
        DeliveryEventRecorder(session).created(point, EventActor.from_claims(claims))
        await session.commit()
        await repository.refresh(point)
        return _json(
            {
                "deliveryPoint": _point_json(point),
                "trackingGuide": point.tracking_token,
                "addressNormalization": normalization.as_dict(),
            },
            status_code=201,
            headers=_NO_STORE,
        )
    except IntegrityError as error:
        await session.rollback()
        if _is_duplicate_order(error):
            return _error("Ya existe un pedido con esa guía.", 409)
        _log_database_error("No se pudo guardar el pedido", error)
        return _error("No se pudo procesar el pedido.", 500)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_database_error("No se pudo guardar el pedido", error)
        return _error("No se pudo procesar el pedido.", 500)


@router.get("/{delivery_point_id}")
async def get_delivery_point(
    delivery_point_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(delivery_point_id)
    if parsed_id is None:
        return _error("El pedido no existe.", 404)
    if claims is None:
        return _error("Debes iniciar sesión para acceder a esta operación.", 401)
    try:
        point = await DeliveryPointRepository(session).get_delivery_point(
            parsed_id, include_route=True
        )
        if point is None:
            return _error("El pedido no existe.", 404)
        if not _is_dispatcher(claims) and not _belongs_to_courier(point, claims):
            return _error("El pedido no existe.", 404)
        return _json(
            {
                "deliveryPoint": _point_json(
                    point,
                    include_route=_is_dispatcher(claims),
                    include_tracking=_is_dispatcher(claims),
                )
            },
            headers=_NO_STORE,
        )
    except SQLAlchemyError as error:
        _log_database_error("No se pudo consultar el pedido", error)
        return _error("No se pudo procesar el pedido.", 500)


@router.patch("/{delivery_point_id}")
async def update_delivery_point(
    delivery_point_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
    advisor: Annotated[AddressAiAdvisor | None, Depends(get_address_ai_advisor)],
) -> JSONResponse:
    parsed_id = _parse_uuid(delivery_point_id)
    if parsed_id is None:
        return _error("El pedido no existe.", 404)
    if claims is None:
        return _error("Debes iniciar sesión para continuar.", 401)

    is_dispatcher = _is_dispatcher(claims)
    courier_id = _session_courier_id(claims)
    if not is_dispatcher and courier_id is None:
        return _error("Se requiere una sesión de despacho.", 403)

    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        data = OrderUpdateRequest.model_validate(body)
    except ValidationError as error:
        return _validation_error(error)
    fields = data.model_fields_set

    try:
        repository = DeliveryPointRepository(session)
        point = await repository.get_delivery_point(parsed_id, include_route=True)
        if point is None:
            return _error("El pedido no existe.", 404)
        if courier_id is not None and not _belongs_to_courier(point, claims):
            return _error("El pedido no existe.", 404)

        # Una solicitud de cliente solo se resuelve en /solicitudes (aceptar o rechazar).
        if point.status is DeliveryPointStatus.REQUESTED:
            return _error("Acepta o rechaza la solicitud antes de modificarla.", 409)
        if "status" in fields and data.status is DeliveryPointStatus.REQUESTED:
            return _error("Un pedido no puede volver a ser solicitud.", 400)

        if (
            is_dispatcher
            and point.route is not None
            and point.route.status in {RouteStatus.PLANNED, RouteStatus.IN_PROGRESS}
            and fields.intersection({"address", "lat", "lng"})
        ):
            return _error(
                "No se puede cambiar la dirección o ubicación con una ruta activa. "
                "Cancela la ruta antes de corregir el pedido y vuelve a optimizar.",
                409,
            )

        if courier_id is not None:
            if fields != {"status"}:
                return _error(
                    "El repartidor solo puede actualizar el estado de sus propias entregas.",
                    403,
                )
            if point.route is None or point.route.status is not RouteStatus.IN_PROGRESS:
                return _error("Solo puedes reportar entregas de una ruta que esté en curso.", 409)
            if point.status not in {
                DeliveryPointStatus.PENDING,
                DeliveryPointStatus.EN_ROUTE,
            }:
                return _error("Esta entrega ya tiene un estado final y no puede modificarse.", 409)
