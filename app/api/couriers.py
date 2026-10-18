"""Operación privada de repartidores, credenciales de acceso y telemetría GPS."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse, Response

from app.api.auth import require_courier_session, require_dispatcher_session
from app.core.auth import SessionClaims, create_courier_access_code, hash_courier_access_code
from app.core.location.courier_location import COURIER_LOCATION_MAX_AGE, is_courier_location_fresh
from app.core.location.pasto_area import is_within_pasto_service_area
from app.core.passwords import hash_password
from app.db.session import get_session, get_session_factory
from app.models import (
    Account,
    Courier,
    CourierCredential,
    CourierStatus,
    LocationEvent,
)
from app.repositories.courier_repository import CourierRepository
from app.schemas.couriers import (
    CourierCollectionResponse,
    CourierCreatedResponse,
    CourierCreateRequest,
    CourierLocationInput,
    CourierResponse,
    CourierSelfView,
    CourierUpdateRequest,
    CourierView,
    LocationEventResponse,
    LocationEventView,
)
from app.services.customer_notifications import (
    any_customer_notification_delivery_configured,
    dispatch_pending_customer_notifications_background,
    enqueue_courier_near_notification,
    is_courier_near_delivery_point,
)
from app.services.location_event_queue import LocationEventQueue, LocationQueueFullError

router = APIRouter(prefix="/repartidores", tags=["repartidores"])
_LOGGER = logging.getLogger(__name__)
_location_queue = LocationEventQueue()


class _LocationWriteResult(TypedDict):
    event: LocationEvent
    accepted: bool
    nearby_notification_queued: bool


def get_location_event_queue() -> LocationEventQueue:
    return _location_queue


def get_write_session_factory() -> async_sessionmaker[AsyncSession]:
    return get_session_factory()


@router.get("/me")
async def get_my_courier(
    claims: Annotated[SessionClaims, Depends(require_courier_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    courier_id = _parse_uuid(
        claims.get("courierId"), "El identificador del repartidor no es válido."
    )
    courier = await CourierRepository(session).get_courier(courier_id)
    if courier is None:
        return _error("El repartidor no existe.", 404)

    view = CourierSelfView.model_validate(courier)
    if not is_courier_location_fresh(courier.last_location_at):
        view = view.model_copy(update={"current_lat": None, "current_lng": None})
    return _json(
        {"courier": view.model_dump(mode="json", by_alias=True)}, headers=_NO_STORE_PRIVATE
    )


@router.post("/me/ubicacion", status_code=201)
async def post_my_location(
    request: Request,
    background_tasks: BackgroundTasks,
    claims: Annotated[SessionClaims, Depends(require_courier_session)],
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_write_session_factory)
    ],
    queue: Annotated[LocationEventQueue, Depends(get_location_event_queue)],
) -> JSONResponse:
    courier_id = _parse_uuid(
        claims.get("courierId"), "El identificador del repartidor no es válido."
    )
    return await _post_location(request, courier_id, session_factory, queue, background_tasks)


@router.delete("/me/ubicacion")
async def stop_my_location(
    claims: Annotated[SessionClaims, Depends(require_courier_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    courier_id = _parse_uuid(
        claims.get("courierId"), "El identificador del repartidor no es válido."
    )
    return await _stop_location(courier_id, session)


@router.get("")
async def list_couriers(
    request: Request,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    status: CourierStatus | None = None
    raw_status = request.query_params.get("status")
    if raw_status is not None:
        try:
            status = CourierStatus(raw_status)
        except ValueError:
            return _error("status debe ser AVAILABLE, ON_ROUTE u OFFLINE.", 400)

    try:
        couriers = await CourierRepository(session).list_couriers(status)
        now = datetime.now(UTC).replace(tzinfo=None)
        views = [_courier_view(courier, now) for courier in couriers]
        payload = CourierCollectionResponse(couriers=views)
        return _json(
            payload.model_dump(mode="json", by_alias=True),
            headers=_NO_STORE_PRIVATE,
        )
    except SQLAlchemyError as error:
        _log_db_error("No se pudo consultar la lista de repartidores", error)
        return _error("No se pudo procesar el repartidor.", 500)


@router.post("", status_code=201)
async def create_courier(
    request: Request,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    if "currentLat" in body or "currentLng" in body:
        return _error(
            "La posición no se define al registrar al repartidor; "
            "debe venir de su GPS autenticado.",
            400,
        )
    if "status" in body and body["status"] != CourierStatus.OFFLINE.value:
        return _error("Un repartidor nuevo queda fuera de turno hasta que reporte GPS válido.", 400)
    try:
        data = CourierCreateRequest.model_validate(body)
    except ValidationError as error:
        return _error(_string_field_error(error), 400)

    courier_id = uuid4()
    access_code = create_courier_access_code(str(courier_id))
    try:
        access_code_hash = await run_in_threadpool(hash_courier_access_code, access_code)
        courier = Courier(
            id=courier_id,
            name=data.name,
            phone=data.phone,
            status=CourierStatus.OFFLINE,
            credential=CourierCredential(access_code_hash=access_code_hash),
        )
        repository = CourierRepository(session)
        repository.add_courier(courier)
        if data.account is not None:
            # Un solo commit evita crear un repartidor sin su acceso si el correo está ocupado.
            await session.flush()
            session.add(
                Account(
                    email=data.account.email,
                    password_hash=await run_in_threadpool(
                        hash_password, data.account.password.get_secret_value()
                    ),
                    role="courier",
                    courier_id=courier_id,
                )
            )
        await session.commit()
        await repository.refresh_courier(courier)
        payload = CourierCreatedResponse(
            courier=CourierView.model_validate(courier),
            access_code=access_code if data.account is None else None,
        )
        return _json(
            payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            status_code=201,
            headers=_NO_STORE_PRIVATE,
        )
    except IntegrityError:
        await session.rollback()
        return _error("Ese correo ya pertenece a otra cuenta.", 409)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_db_error("No se pudo crear el repartidor", error)
        return _error("No se pudo procesar el repartidor.", 500)


@router.get("/{courier_id}")
async def get_courier(
    courier_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(courier_id)
    courier = await CourierRepository(session).get_courier(parsed_id)
    if courier is None:
        return _error("El repartidor no existe.", 404)
    return _json(
        CourierResponse(courier=CourierView.model_validate(courier)).model_dump(
            mode="json", by_alias=True
        )
    )


@router.patch("/{courier_id}")
async def update_courier(
    courier_id: str,
    request: Request,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(courier_id)
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    if "currentLat" in body or "currentLng" in body:
        return _error(
            "La ubicación solo se actualiza desde el endpoint GPS autenticado del repartidor.",
            400,
        )
    try:
        data = CourierUpdateRequest.model_validate(body)
    except ValidationError as error:
        if any(item["loc"] == ("status",) for item in error.errors()):
            return _error("status debe ser AVAILABLE, ON_ROUTE u OFFLINE.", 400)
        return _error(_string_field_error(error), 400)

    fields_set = data.model_fields_set
    values: dict[str, str | CourierStatus] = {}
    for field in ("name", "phone"):
        if field in fields_set:
            value = getattr(data, field)
            if not isinstance(value, str) or not value:
                return _error(f"{field} debe ser un string no vacío.", 400)
            values[field] = value
    if "status" in fields_set:
        if data.status is not CourierStatus.OFFLINE:
            return _error(
                "Disponible y En ruta se determinan con GPS autenticado "
                "y el ciclo de vida de la ruta.",
                400,
            )
        values["status"] = CourierStatus.OFFLINE
    if not values:
        return _error("No hay campos válidos para actualizar.", 400)

    courier = await CourierRepository(session).get_courier(parsed_id)
    if courier is None:
        return _error("El repartidor no existe.", 404)
    for field, value in values.items():
        setattr(courier, field, value)
    try:
        await session.commit()
        await CourierRepository(session).refresh_courier(courier)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_db_error("No se pudo actualizar el repartidor", error)
        return _error("No se pudo procesar el repartidor.", 500)
    return _json(
        CourierResponse(courier=CourierView.model_validate(courier)).model_dump(
            mode="json", by_alias=True
        )
    )


@router.delete("/{courier_id}", status_code=204)
async def delete_courier(
    courier_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    parsed_id = _parse_uuid(courier_id)
    repository = CourierRepository(session)
    courier = await repository.get_courier(parsed_id)
    if courier is None:
        return _error("El repartidor no existe.", 404)
    if await repository.has_routes(parsed_id):
        return _error("No se puede eliminar un repartidor con rutas asociadas.", 409)
    try:
        await repository.delete_courier(courier)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _error("No se puede eliminar un repartidor con rutas asociadas.", 409)
    except SQLAlchemyError as error:
        await session.rollback()
        _log_db_error("No se pudo eliminar el repartidor", error)
        return _error("No se pudo procesar el repartidor.", 500)
    return Response(status_code=204)


@router.post("/{courier_id}/codigo")
async def renew_courier_access_code(
    courier_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(courier_id)
    repository = CourierRepository(session)
    courier = await repository.get_courier(parsed_id)
    if courier is None:
        return _error("El repartidor no existe.", 404)
    access_code = create_courier_access_code(str(parsed_id))
    try:
        access_code_hash = await run_in_threadpool(hash_courier_access_code, access_code)
        credential = await repository.get_credential(parsed_id)
        if credential is None:
            repository.add_credential(
                CourierCredential(courier_id=parsed_id, access_code_hash=access_code_hash)
            )
        else:
            credential.access_code_hash = access_code_hash
            credential.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        _log_db_error("No se pudo renovar el código del repartidor", error)
        return _error("No se pudo procesar el repartidor.", 500)
    return _json({"accessCode": access_code}, headers=_NO_STORE_PRIVATE)


@router.post("/{courier_id}/ubicacion", status_code=201)
async def post_courier_location(
    courier_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    claims: Annotated[SessionClaims, Depends(require_courier_session)],
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_write_session_factory)
    ],
    queue: Annotated[LocationEventQueue, Depends(get_location_event_queue)],
) -> JSONResponse:
    parsed_id = _parse_uuid(courier_id)
    if str(parsed_id) != claims.get("courierId"):
        return _error("No puedes actualizar la ubicación de otro repartidor.", 403)
    return await _post_location(request, parsed_id, session_factory, queue, background_tasks)


async def _post_location(
    request: Request,
    courier_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    queue: LocationEventQueue,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    body = await _read_json_object(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        location = CourierLocationInput.model_validate(body)
    except ValidationError as error:
        if any(item["loc"] == ("recorded_at",) for item in error.errors()):
            return _error("recordedAt debe ser una fecha ISO válida.", 400)
        return _error("lat debe estar entre -90 y 90 y lng entre -180 y 180.", 400)

    if not math.isfinite(location.lat) or not math.isfinite(location.lng):
        return _error("lat debe estar entre -90 y 90 y lng entre -180 y 180.", 400)
    if not is_within_pasto_service_area(location.lat, location.lng):
        return _error("La ubicación debe estar dentro de la zona de servicio de Pasto.", 400)
    recorded_at = _parse_recorded_at(location.recorded_at)
    if isinstance(recorded_at, JSONResponse):
        return recorded_at

    try:
        result = await queue.enqueue(
