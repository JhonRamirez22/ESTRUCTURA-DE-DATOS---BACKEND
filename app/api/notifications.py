"""Consulta segura del buzón operativo del despachador o repartidor autenticado."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import get_optional_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.models import CustomerNotificationChannel
from app.repositories.customer_notification_repository import CustomerNotificationRepository
from app.repositories.notification_repository import NotificationRepository
from app.schemas.notifications import (
    CustomerNotificationChannelStatus,
    CustomerNotificationOperationsStatus,
    NotificationListResponse,
    NotificationView,
)
from app.services.customer_notifications import customer_notification_channels_configured
from app.settings import get_settings

router = APIRouter(prefix="/notificaciones", tags=["notificaciones"])
_LOGGER = logging.getLogger(__name__)
_UNAVAILABLE_WARNING = (
    "Los avisos están temporalmente indisponibles; las operaciones siguen activas."
)


@router.get("", response_model=NotificationListResponse, response_model_by_alias=True)
async def list_notifications(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para consultar los avisos.", 401)

    recipient_id = _authorized_recipient(claims)
    requested_recipient = request.query_params.get("recipientId")
    if requested_recipient is not None and requested_recipient != recipient_id:
        return _error("No puedes consultar avisos de otro usuario.", 403)
    limit = _parse_limit(request.query_params.get("limit"))

    try:
        records = await NotificationRepository(session).list_for_recipient(recipient_id, limit)
        payload = NotificationListResponse(
            notifications=[NotificationView.model_validate(item) for item in records]
        )
        return JSONResponse(
            content=payload.model_dump(mode="json", by_alias=True, exclude_none=True),
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    except Exception as error:
        _LOGGER.warning(
            "No se pudieron leer los avisos operativos",
            extra={"error_type": type(error).__name__},
        )
        return JSONResponse(
            content={"notifications": [], "warning": _UNAVAILABLE_WARNING},
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )


@router.get("/clientes/estado", response_model=CustomerNotificationOperationsStatus)
async def customer_notification_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    claims: Annotated[SessionClaims | None, Depends(get_optional_session)],
) -> JSONResponse:
    if claims is None:
        return _error("Debes iniciar sesión para consultar el estado de avisos.", 401)
    if claims["role"] != "dispatcher":
        return _error("Se requiere una sesión de despacho.", 403)

    try:
        counts = await CustomerNotificationRepository(session).active_outbox_counts()
    except Exception as error:
        _LOGGER.warning(
            "No se pudo consultar el estado del outbox de avisos",
            extra={"error_type": type(error).__name__},
        )
        return _error("No se pudo consultar el estado de avisos al cliente.", 503)

    configured = customer_notification_channels_configured(get_settings())
    worker_task = getattr(request.app.state, "customer_notification_worker_task", None)
    worker_running = worker_task is not None and not worker_task.done()
    channels: dict[Literal["email", "sms"], CustomerNotificationChannelStatus] = {
        "email": CustomerNotificationChannelStatus(
            configured=configured[CustomerNotificationChannel.EMAIL],
            pending=counts[CustomerNotificationChannel.EMAIL]["pending"],
            retrying=counts[CustomerNotificationChannel.EMAIL]["retrying"],
            processing=counts[CustomerNotificationChannel.EMAIL]["processing"],
            failed=counts[CustomerNotificationChannel.EMAIL]["failed"],
        ),
        "sms": CustomerNotificationChannelStatus(
            configured=configured[CustomerNotificationChannel.SMS],
            pending=counts[CustomerNotificationChannel.SMS]["pending"],
