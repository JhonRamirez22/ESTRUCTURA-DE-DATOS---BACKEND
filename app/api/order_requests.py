"""Solicitudes de envío de clientes: entran en espera y despacho las acepta o rechaza.

La creación es pública, así que se protege por capas: origen obligatorio, contrato
estricto sin datos personales, zona de servicio de Pasto, límites compartidos y un tope
de solicitudes en espera. Una solicitud nunca entra a asignación: solo los pedidos en
PENDING son elegibles, y aceptar es la única vía para llegar a ese estado.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import _configured_secret, _validate_origin, require_dispatcher_session
from app.core.auth import SessionClaims
from app.core.location.pasto_area import (
    contains_foreign_city_reference,
    is_within_pasto_service_area,
    normalize_pasto_address,
)
from app.db.session import get_session
from app.models import DeliveryPoint, DeliveryPointStatus
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.schemas.orders import OrderRequestCreate
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.settings import get_settings

router = APIRouter(prefix="/solicitudes", tags=["solicitudes"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}
MAX_REQUEST_BODY_BYTES = 2_048
# Tope de la bandeja: por encima, se rechazan nuevas solicitudes hasta que despacho resuelva.
MAX_WAITING_REQUESTS = 25
# Por cliente (cabecera de reenvío) y para todo el servicio, compartidos entre réplicas.
_CLIENT_LIMITER = CustomerChatRateLimiter(max_requests=3, window_seconds=600)
_SERVICE_LIMITER = CustomerChatRateLimiter(max_requests=20, window_seconds=600)


@router.post("", status_code=201)
async def create_order_request(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    _validate_origin(request, required=True)
    secret = _configured_secret(get_settings())
    if secret is None:
        return _error("El servicio de solicitudes no está configurado.", 503)

    raw = await request.body()
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        return _error("La solicitud es demasiado grande.", 413)
    try:
        body: object = json.loads(raw)
        data = OrderRequestCreate.model_validate(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("El body debe ser JSON válido.", 400)
    except ValidationError:
        return _error(
            "Escribe una dirección de al menos 5 caracteres y marca el punto en el mapa.", 400
        )

    address = normalize_pasto_address(data.address)
    if not is_within_pasto_service_area(data.lat, data.lng):
        return _error("Por ahora solo atendemos direcciones dentro de Pasto, Nariño.", 400)
    if contains_foreign_city_reference(address):
        return _error("La dirección indica una ciudad fuera de Pasto, Nariño.", 400)

    for limiter, key in (
        (_CLIENT_LIMITER, f"solicitud:{_client_key(request)}"),
        (_SERVICE_LIMITER, "solicitud:servicio"),
    ):
        retry_after = await limiter.retry_after_shared(session, key, hmac_secret=secret)
        if retry_after is not None:
            await session.commit()
            return JSONResponse(
                {"error": "Recibimos varias solicitudes seguidas. Intenta en unos minutos."},
                status_code=429,
                headers={**_NO_STORE, "Retry-After": str(retry_after)},
            )

    repository = DeliveryPointRepository(session)
    try:
        if await repository.count_requested() >= MAX_WAITING_REQUESTS:
            await session.commit()
            return _error(
                "Hay muchas solicitudes en revisión. Intenta de nuevo más tarde.", 503
            )
        point = DeliveryPoint(
            id=uuid4(),
            address=address,
            lat=data.lat,
            lng=data.lng,
            status=DeliveryPointStatus.REQUESTED,
            order_id=f"SOL-{secrets.token_hex(3).upper()}",
            tracking_token=str(uuid4()),
        )
        repository.add(point)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _error("No se pudo registrar la solicitud. Intenta de nuevo.", 409)
    except SQLAlchemyError as error:
        await session.rollback()
        _LOGGER.warning(
            "No se pudo guardar la solicitud", extra={"error_type": type(error).__name__}
        )
        return _error("No se pudo registrar la solicitud.", 500)

    return JSONResponse(
        {
            "trackingGuide": point.tracking_token,
            "reference": point.order_id,
            "status": "REQUESTED",
        },
        status_code=201,
        headers=_NO_STORE,
    )


@router.post("/{request_id}/aceptar")
async def accept_order_request(
    request_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(request_id)
    if parsed_id is None:
        return _error("La solicitud no existe.", 404)
    try:
        accepted = await DeliveryPointRepository(session).accept_request(parsed_id)
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        _LOGGER.warning(
            "No se pudo aceptar la solicitud", extra={"error_type": type(error).__name__}
        )
        return _error("No se pudo procesar la solicitud.", 500)
    if not accepted:
        return _error("La solicitud ya fue resuelta o no existe.", 409)
    return JSONResponse({"ok": True, "status": "PENDING"}, headers=_NO_STORE)


@router.post("/{request_id}/rechazar")
async def reject_order_request(
    request_id: str,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    parsed_id = _parse_uuid(request_id)
    if parsed_id is None:
        return _error("La solicitud no existe.", 404)
    try:
        rejected = await DeliveryPointRepository(session).reject_request(parsed_id)
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        _LOGGER.warning(
            "No se pudo rechazar la solicitud", extra={"error_type": type(error).__name__}
        )
        return _error("No se pudo procesar la solicitud.", 500)
    if not rejected:
        return _error("La solicitud ya fue resuelta o no existe.", 409)
    return JSONResponse({"ok": True}, headers=_NO_STORE)


def _client_key(request: Request) -> str:
    """Primer salto declarado por los proxies; si falta, la conexión directa.

    Es falsificable por un cliente decidido, por eso el límite global y el tope de la
    bandeja son los controles que acotan el abuso de verdad.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()[:64]
    if first:
        return first
    return request.client.host if request.client else "desconocido"


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code, headers=_NO_STORE)
