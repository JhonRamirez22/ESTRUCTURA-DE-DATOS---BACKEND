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
