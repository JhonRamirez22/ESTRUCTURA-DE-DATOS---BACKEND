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
