"""Solicitudes de envío de clientes: entran en espera y despacho las acepta o rechaza.

La creación es pública, así que se protege por capas: origen obligatorio, contrato
estricto sin datos personales, zona de servicio de Pasto, límites compartidos y un tope
de solicitudes en espera. Una solicitud nunca entra a asignación: solo los pedidos en
PENDING son elegibles, y aceptar es la única vía para llegar a ese estado.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from app.core.location.proximity import haversine_m
from app.core.structures.lru_cache import LruCache
from app.db.session import get_session
from app.models import DeliveryPoint, DeliveryPointStatus
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.schemas.orders import OrderRequestCreate
from app.services.address_advisor import AddressAiAdvisor, get_address_ai_advisor
from app.services.address_geocoder import (
    AddressGeocoder,
    AddressGeocoderError,
    get_address_geocoder,
)
from app.services.address_resolver import resolve_address
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
# Búsquedas de dirección: más holgadas que el envío, pero acotan el gasto en TomTom y Groq.
_LOOKUP_CLIENT_LIMITER = CustomerChatRateLimiter(max_requests=15, window_seconds=600)
_LOOKUP_SERVICE_LIMITER = CustomerChatRateLimiter(max_requests=120, window_seconds=600)
# Respuestas recientes por texto normalizado: repetir una búsqueda no vuelve a los proveedores.
_LOOKUP_CACHE: LruCache[str, dict[str, object]] = LruCache(256)


class AddressLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=3, max_length=160)


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
    nearby = await _nearby_stop_notice(repository, data.lat, data.lng)
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
            # Solo distancia y estado: nunca datos de otros clientes.
            "nearbyStop": nearby,
        },
        status_code=201,
        headers=_NO_STORE,
    )


@router.post("/direccion")
async def lookup_address(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    geocoder: Annotated[AddressGeocoder | None, Depends(get_address_geocoder)],
    advisor: Annotated[AddressAiAdvisor | None, Depends(get_address_ai_advisor)],
) -> JSONResponse:
    """Deduce la dirección de un cliente: TomTom primero y la IA solo si no hay coincidencias.

    La IA interpreta el texto; las coordenadas salen siempre del geocodificador.
    """
    _validate_origin(request, required=True)
    secret = _configured_secret(get_settings())
    if secret is None or geocoder is None:
        return _error(
            "La búsqueda de direcciones no está disponible; marca el punto en el mapa.", 503
        )
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        return _error("La solicitud es demasiado grande.", 413)
    try:
        data = AddressLookupRequest.model_validate(json.loads(raw))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        return _error("Escribe una dirección de 3 a 160 caracteres.", 400)
    if "<" in data.address or ">" in data.address:
        return _error("La dirección contiene caracteres no permitidos.", 400)

    cache_key = " ".join(normalize_pasto_address(data.address).casefold().split())
    cached = _LOOKUP_CACHE.get(cache_key)
    if cached is not None:
        return JSONResponse({**cached, "cached": True}, headers=_NO_STORE)

    for limiter, key in (
        (_LOOKUP_CLIENT_LIMITER, f"direccion:{_client_key(request)}"),
        (_LOOKUP_SERVICE_LIMITER, "direccion:servicio"),
    ):
        retry_after = await limiter.retry_after_shared(session, key, hmac_secret=secret)
        if retry_after is not None:
            await session.commit()
            return JSONResponse(
                {"error": "Hiciste varias búsquedas seguidas. Marca el punto en el mapa o espera."},
                status_code=429,
                headers={**_NO_STORE, "Retry-After": str(retry_after)},
            )
    await session.commit()
    try:
        result = await resolve_address(data.address, geocoder, advisor)
    except AddressGeocoderError:
        return _error("La búsqueda no respondió; marca el punto en el mapa.", 503)
    _LOOKUP_CACHE.put(cache_key, result)
    return JSONResponse({**result, "cached": False}, headers=_NO_STORE)


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


NEARBY_STOP_RADIUS_M = 150.0


async def _nearby_stop_notice(
    repository: DeliveryPointRepository, lat: float, lng: float
) -> dict[str, object] | None:
    """Aviso discreto si la dirección queda cerca de una parada ya programada."""
    lat_delta = NEARBY_STOP_RADIUS_M / 111_000
    lng_delta = NEARBY_STOP_RADIUS_M / (111_000 * max(0.2, math.cos(math.radians(lat))))
    points = await repository.active_points_near(lat, lng, lat_delta, lng_delta)
    best: tuple[DeliveryPoint, float] | None = None
    for point in points:
        distance = haversine_m(lat, lng, point.lat, point.lng)
        if distance < NEARBY_STOP_RADIUS_M and (best is None or distance < best[1]):
            best = (point, distance)
    if best is None:
        return None
    point, distance = best
    return {
        "distanceMeters": round(distance),
        "status": point.status.value,
        "message": (
            "Hay una parada programada a menos de 150 m; "
            "el despacho puede sumar esta entrega a esa ruta."
        ),
    }


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
