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
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_normalization import AddressNormalizationResult, normalize_address
from app.services.customer_notifications import cancel_pending_customer_notifications

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
            return _error("status debe ser PENDING, EN_ROUTE, DELIVERED o FAILED.", 400)

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
            if data.status not in {
                DeliveryPointStatus.DELIVERED,
                DeliveryPointStatus.FAILED,
            }:
                return _error("El estado de entrega no es válido para el repartidor.", 400)

        next_customer_email = (
            data.customer_email if "customer_email" in fields else point.customer_email
        )
        next_email_notifications_enabled = (
            data.email_notifications_enabled
            if data.email_notifications_enabled is not None
            else point.email_notifications_enabled
        )
        if not next_email_notifications_enabled:
            next_customer_email = None
        if next_email_notifications_enabled and not next_customer_email:
            return _error("Se requiere customerEmail para activar avisos por correo.", 400)
        next_sms_notifications_enabled = (
            data.sms_notifications_enabled
            if data.sms_notifications_enabled is not None
            else point.sms_notifications_enabled
        )
        next_customer_phone = (
            data.customer_phone if "customer_phone" in fields else point.customer_phone
        )
        if not next_sms_notifications_enabled:
            next_customer_phone = None
        if next_sms_notifications_enabled and not next_customer_phone:
            return _error("Se requiere customerPhone para activar avisos por SMS.", 400)
        customer_notification_consent_changed = (
            next_customer_email != point.customer_email
            or next_email_notifications_enabled != point.email_notifications_enabled
            or next_customer_phone != point.customer_phone
            or next_sms_notifications_enabled != point.sms_notifications_enabled
        )

        previous_status = point.status
        next_address = normalize_pasto_address(data.address or point.address)
        next_latitude = point.lat if data.lat is None else data.lat
        next_longitude = point.lng if data.lng is None else data.lng
        invalid_location = _validate_pasto_location(
            next_address, next_latitude, next_longitude
        )
        if invalid_location:
            return _error(invalid_location, 400)

        normalization: AddressNormalizationResult | None = None
        if data.address is not None:
            normalization = await normalize_address(
                next_address, next_latitude, next_longitude, advisor
            )
            next_address = normalization.address

        updates: dict[str, object] = {}
        if "address" in fields:
            updates["address"] = next_address
        if data.lat is not None:
            updates["lat"] = data.lat
        if data.lng is not None:
            updates["lng"] = data.lng
        if data.order_id is not None:
            updates["order_id"] = data.order_id
        if "status" in fields and data.status is not None:
            updates["status"] = data.status
        if "time_window" in fields:
            updates["time_window"] = data.time_window
        if "customer_email" in fields or not next_email_notifications_enabled:
            updates["customer_email"] = next_customer_email
        if data.email_notifications_enabled is not None:
            updates["email_notifications_enabled"] = data.email_notifications_enabled
        if "customer_phone" in fields or not next_sms_notifications_enabled:
            updates["customer_phone"] = next_customer_phone
        if data.sms_notifications_enabled is not None:
            updates["sms_notifications_enabled"] = data.sms_notifications_enabled

        updated_in_place = await repository.try_update_delivery_point(
            point_id=parsed_id,
            expected_status=point.status,
            expected_route_id=point.route_id,
            updates=updates,
            courier_id=UUID(courier_id) if courier_id is not None else None,
        )
        if not updated_in_place:
            await session.rollback()
            return _error(
                "El pedido o la ruta cambió mientras se procesaba; actualiza la pantalla "
                "e inténtalo de nuevo.",
                409,
            )

        # El UPDATE con RETURNING puede sincronizar los atributos ORM; compara con el estado
        # original capturado antes para cancelar avisos pendientes al revocar consentimiento.
        if customer_notification_consent_changed:
            await cancel_pending_customer_notifications(session, parsed_id)

        if (
            previous_status != data.status
            and data.status in {DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED}
        ):
            NotificationRepository(session).add(
                Notification(
                    id=uuid4(),
                    kind=NotificationKind.DELIVERY_STATUS_CHANGED,
                    recipient_id="dispatcher",
                    title="Estado de entrega actualizado",
                    message=f"El pedido {point.order_id} quedó en estado {data.status.value}.",
                )
            )
        await session.commit()
        updated = await repository.get_delivery_point(parsed_id, include_route=True)
        if updated is None:
            return _error("El pedido no existe.", 404)

        content: dict[str, object] = {
            "deliveryPoint": _point_json(
                updated,
