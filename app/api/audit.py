"""Auditoría de datos para el despacho: buscar un sujeto y descargar su informe en PDF."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.api.auth import require_dispatcher_session
from app.core.auth import SessionClaims
from app.db.session import get_session
from app.models import Courier, DeliveryEvent, DeliveryPoint
from app.models.account import Account
from app.models.security import SecurityAuditEvent
from app.services.audit_report import (
    AuditPeriodError,
    ReportData,
    collect_account_report,
    collect_courier_report,
    collect_order_report,
    mask_email,
    period_bounds,
    render_report,
)
from app.services.fleet_reports import FleetReportBuilder, ReportKind
from app.services.request_limiter import SlidingWindowLimiter

router = APIRouter(prefix="/auditoria", tags=["auditoria"])
_NO_STORE = {"Cache-Control": "no-store, private, max-age=0"}
_LIMITER = SlidingWindowLimiter(limit=10, window_seconds=60.0)
_SEARCH_LIMIT = 30
Kind = Literal["courier", "account", "order"]


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/sujetos")
async def list_subjects(
    _claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tipo: Annotated[Kind, Query()],
    q: Annotated[str, Query(max_length=80)] = "",
) -> JSONResponse:
    term = q.strip()
    try:
        if tipo == "courier":
            stmt_c = select(Courier).order_by(Courier.name.asc()).limit(_SEARCH_LIMIT)
            if term:
                stmt_c = stmt_c.where(Courier.name.ilike(_like(term), escape="\\"))
            rows = (await session.scalars(stmt_c)).all()
            items = [
                {"id": str(row.id), "label": row.name, "detail": row.status.value} for row in rows
            ]
        elif tipo == "account":
            stmt_a = (
                select(Account).where(Account.role == "dispatcher")
