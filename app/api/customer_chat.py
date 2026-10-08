"""Chat público con acceso exclusivo mediante guía de seguimiento."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse

from app.core.routing.cached_matrix_provider import CachedMatrixProvider
from app.core.routing.cached_route_provider import CachedRouteProvider
from app.db.session import get_session
from app.repositories.delivery_point_repository import DeliveryPointRepository
from app.schemas.customer_chat import CustomerChatRequest, CustomerChatResponse
from app.services.customer_chat_rate_limiter import CustomerChatRateLimiter
from app.services.customer_order_chat import (
    CustomerChatProviderError,
    OrderChatAssistant,
    build_order_fallback_answer,
    customer_asks_for_arrival_estimate,
    get_customer_order_chat,
)
from app.services.customer_order_eta import estimate_customer_order_eta
from app.services.route_planner import get_routing_providers
from app.services.traffic import TrafficService, get_traffic_service
from app.settings import get_settings

router = APIRouter(prefix="/cliente/chat", tags=["asistente de pedidos"])
_LOGGER = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}
_CHAT_RATE_LIMITER = CustomerChatRateLimiter(max_requests=12, window_seconds=60)
_MAX_CHAT_REQUEST_BYTES = 16_384
_PROVIDER_NOTICE = (
    "Privacidad: la IA recibe solo la intención normalizada y el estado; el texto original no se "
    "comparte. No se envían guía, dirección, coordenadas, nombre, teléfono ni IDs. "
    "Si preguntas por la llegada, el ETA es aproximado; TomTom, si está disponible, solo cubre "
    "parte del primer tramo, no toda la ruta. El chat se limita a este pedido."
)


@router.post("")
async def chat_about_order(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    assistant: Annotated[OrderChatAssistant | None, Depends(get_customer_order_chat)],
    routing_providers: Annotated[
        tuple[CachedMatrixProvider, CachedRouteProvider],
        Depends(get_routing_providers),
    ],
    traffic_provider: Annotated[TrafficService | None, Depends(get_traffic_service)],
) -> JSONResponse:
    raw_body = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > _MAX_CHAT_REQUEST_BYTES:
                return _error("La solicitud del chat supera el tamaño permitido.", 413)
            raw_body.extend(chunk)
        body: object = json.loads(raw_body)
        if not isinstance(body, dict):
            return _error("El body debe ser un objeto JSON.", 400)
        data = CustomerChatRequest.model_validate(body)
    except ClientDisconnect:
        return _error("La solicitud se interrumpió antes de completarse.", 400)
    except (ValueError, UnicodeDecodeError, ValidationError):
        return _error("La guía o los mensajes no tienen un formato válido.", 400)

    if assistant is None:
        return _error(
            "El asistente no está configurado. Puedes seguir consultando el pedido con tu guía.",
            503,
        )

    settings = get_settings()
    auth_secret = (
        settings.auth_secret.get_secret_value()
        if settings.auth_secret is not None
        else ""
    )
    try:
        retry_after = await _CHAT_RATE_LIMITER.retry_after_shared(
            session,
            data.guide,
            hmac_secret=auth_secret,
        )
    except (SQLAlchemyError, ValueError, RuntimeError) as error:
        await session.rollback()
        _LOGGER.warning(
            "No se pudo aplicar el límite compartido del chat de pedido",
            extra={"error_type": type(error).__name__},
        )
        return _error("El asistente no está disponible temporalmente.", 503)

    if retry_after is not None:
        return JSONResponse(
            content={"error": "Has enviado varias preguntas seguidas. Espera un momento."},
            status_code=429,
            headers={**_NO_STORE, "Retry-After": str(retry_after)},
        )

    try:
        point = await DeliveryPointRepository(session).get_delivery_point_by_tracking_token(
            data.guide
        )
    except SQLAlchemyError as error:
        _LOGGER.warning(
            "No se pudo cargar el contexto mínimo para el chat de pedido",
            extra={"error_type": type(error).__name__},
        )
        return _error("El asistente no está disponible temporalmente.", 503)

    if point is None:
        return _error("No encontramos un pedido con esa guía.", 404)

    estimated_minutes_from_now: float | None = None
    estimate_basis = "No hay un ETA vial confiable disponible para este pedido."
    if customer_asks_for_arrival_estimate(data.messages):
        try:
            eta_result = await estimate_customer_order_eta(
                point,
                routing_providers[0],
                routing_providers[1],
                traffic_provider=traffic_provider,
            )
            if eta_result is not None:
                estimated_minutes_from_now = eta_result.estimated_minutes_from_now
                if eta_result.traffic_applied:
                    estimate_basis = (
                        "Se aplicó una señal TomTom parcial solo al primer tramo; los tramos "
                        "posteriores usan tiempos viales base, no tráfico completo."
                    )
                elif eta_result.traffic_configured:
                    estimate_basis = (
                        "No se aplicó tráfico TomTom; usar tiempos viales base. "
                        f"Motivo: {eta_result.traffic_warning or eta_result.traffic_status}."
                    )
                else:
                    estimate_basis = (
                        "TomTom no está configurado; el ETA usa tiempos viales base."
                    )
        except Exception as error:
            _LOGGER.warning(
                "No se pudo calcular el ETA para la pregunta del cliente",
                extra={"error_type": type(error).__name__},
            )

    provider_notice = _PROVIDER_NOTICE
    try:
        reply = await assistant.answer(
            delivery_status=point.status.value,
            route_status=point.route.status.value if point.route is not None else None,
            estimated_minutes_from_now=estimated_minutes_from_now,
            estimate_basis=estimate_basis,
            messages=data.messages,
        )
    except CustomerChatProviderError as error:
        _LOGGER.warning(
            "Se usó la respuesta local de respaldo para el chat de pedidos",
            extra={"error_type": type(error).__name__},
        )
        reply = build_order_fallback_answer(
            delivery_status=point.status.value,
            route_status=point.route.status.value if point.route is not None else None,
            estimated_minutes_from_now=estimated_minutes_from_now,
            messages=data.messages,
        )
        provider_notice = (
            f"{_PROVIDER_NOTICE} En esta respuesta, la IA no estuvo disponible; "
            "el respaldo se generó localmente con el estado verificado del pedido."
        )

    payload = CustomerChatResponse(reply=reply, provider_notice=provider_notice)
    return JSONResponse(
        content=payload.model_dump(mode="json", by_alias=True),
        headers=_NO_STORE,
    )


@router.get("/estado")
async def customer_chat_status(
    assistant: Annotated[OrderChatAssistant | None, Depends(get_customer_order_chat)],
) -> JSONResponse:
    """Expose whether the provider is configured, without exposing its configuration."""
    return JSONResponse(
        content={"configured": assistant is not None},
        headers=_NO_STORE,
    )


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        content={"error": message},
        status_code=status_code,
        headers=_NO_STORE,
    )
