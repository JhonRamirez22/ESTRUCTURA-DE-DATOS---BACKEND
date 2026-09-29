"""Shipment history for dispatch: filtered list and per-shipment timeline."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.models import DeliveryPointStatus
from app.services.audit_report import AuditPeriodError, period_bounds
from app.services.delivery_history import DeliveryHistoryReader, HistoryFilters

router = APIRouter(prefix="/historial", tags=["historial"])
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="seconds") + "Z"


@router.get("/envios")
async def list_shipments(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    desde: Annotated[date | None, Query()] = None,
    hasta: Annotated[date | None, Query()] = None,
    estado: Annotated[DeliveryPointStatus | None, Query()] = None,
    repartidor: Annotated[UUID | None, Query()] = None,
    q: Annotated[str, Query(max_length=80)] = "",
    programados: Annotated[bool, Query()] = False,
    pagina: Annotated[int, Query(ge=1, le=10_000)] = 1,
    tamano: Annotated[int, Query(ge=1, le=50)] = 25,
) -> JSONResponse:
    begin = end = None
    if desde is not None or hasta is not None:
        try:
            first = desde or hasta or date.today()
            begin, end = period_bounds(first, hasta or first)
        except AuditPeriodError as error:
            return JSONResponse({"error": str(error)}, status_code=400, headers=_NO_STORE)
    filters = HistoryFilters(begin, end, estado, repartidor, q, programados)
    try:
        page = await DeliveryHistoryReader(session).search(filters, page=pagina, page_size=tamano)
    except SQLAlchemyError:
        return JSONResponse({"error": "No se pudo leer el historial."}, status_code=500,
                            headers=_NO_STORE)
    return JSONResponse(
        {
            "total": page.total,
            "page": page.page,
            "pageSize": page.page_size,
            "items": [
                {
                    "id": str(item.point_id),
                    "orderId": item.order_ref,
                    "address": item.address,
                    "status": item.status,
                    "courierName": item.courier_name,
                    "createdAt": iso(item.created_at),
                    "deliveredAt": iso(item.delivered_at),
                    "scheduledFor": iso(item.scheduled_for),
                    "events": item.events,
                    "lastEventAt": iso(item.last_event_at),
                }
                for item in page.items
            ],
        },
        headers=_NO_STORE,
    )


@router.get("/envios/{point_id}/eventos")
async def shipment_timeline(
    point_id: UUID,
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> JSONResponse:
    try:
        entries = await DeliveryHistoryReader(session).timeline(point_id)
    except SQLAlchemyError:
        return JSONResponse({"error": "No se pudo leer el historial."}, status_code=500,
                            headers=_NO_STORE)
    if entries is None:
        return JSONResponse({"error": "El envío no existe."}, status_code=404, headers=_NO_STORE)
    return JSONResponse(
        {
            "events": [
                {
                    "type": e.event_type,
                    "at": iso(e.occurred_at),
                    "fromStatus": e.from_status,
                    "toStatus": e.to_status,
                    "actor": e.actor_role,
                    "courierName": e.courier_name,
                    "detail": e.detail,
                }
                for e in entries
            ]
        },
        headers=_NO_STORE,
    )
