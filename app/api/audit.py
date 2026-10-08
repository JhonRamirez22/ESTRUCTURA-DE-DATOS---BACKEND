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
                .order_by(Account.email.asc()).limit(_SEARCH_LIMIT)
            )
            if term:
                stmt_a = stmt_a.where(Account.email.ilike(_like(term), escape="\\"))
            rows_a = (await session.scalars(stmt_a)).all()
            items = [
                {"id": str(row.id), "label": row.email,
                 "detail": "Habilitada" if row.enabled else "Deshabilitada"}
                for row in rows_a
            ]
        else:
            stmt_o = select(DeliveryPoint).order_by(DeliveryPoint.id.desc()).limit(_SEARCH_LIMIT)
            if term:
                stmt_o = stmt_o.where(
                    or_(
                        DeliveryPoint.order_id.ilike(_like(term), escape="\\"),
                        DeliveryPoint.address.ilike(_like(term), escape="\\"),
                    )
                )
            rows_o = (await session.scalars(stmt_o)).all()
            items = [
                {"id": str(row.id), "label": row.order_id,
                 "detail": f"{row.status.value} · {row.address[:60]}"
                 + (f" · {mask_email(row.customer_email)}" if row.customer_email else "")}
                for row in rows_o
            ]
    except SQLAlchemyError:
        return JSONResponse({"error": "No se pudo buscar."}, status_code=500, headers=_NO_STORE)
    return JSONResponse({"subjects": items}, headers=_NO_STORE)


@router.get("/informe")
async def download_report(
    request: Request,
    claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tipo: Annotated[Kind, Query()],
    id: Annotated[UUID, Query()],  # noqa: A002 - nombre público del parámetro
    desde: Annotated[date, Query()],
    hasta: Annotated[date, Query()],
) -> Response:
    account_raw = claims.get("accountId")
    requester = UUID(account_raw) if account_raw else None
    retry = _LIMITER.retry_after(hashlib.sha256((account_raw or "dispatcher").encode()).hexdigest())
    if retry is not None:
        return JSONResponse(
            {"error": "Demasiados informes seguidos. Intenta en unos segundos."},
            status_code=429, headers={**_NO_STORE, "Retry-After": str(retry)},
        )
    try:
        begin, end = period_bounds(desde, hasta)
    except AuditPeriodError as error:
        return JSONResponse({"error": str(error)}, status_code=400, headers=_NO_STORE)
    try:
        data: ReportData | None
        if tipo == "courier":
            data = await collect_courier_report(session, id, begin, end)
        elif tipo == "account":
            data = await collect_account_report(session, id, begin, end)
        else:
            data = await collect_order_report(session, id, begin, end)
        if data is None:
            return JSONResponse({"error": "El sujeto no existe."}, status_code=404,
                                headers=_NO_STORE)
        generated_by = "cuenta de despacho"
        if requester is not None:
            account = await session.get(Account, requester)
            generated_by = account.email if account else generated_by
        now = datetime.now(UTC).replace(tzinfo=None)
        # Quién descargó qué queda en el propio registro de auditoría (sin datos del sujeto).
        session.add(SecurityAuditEvent(
            account_id=requester, action=f"audit_report:{tipo}", outcome="allowed",
            request_id=getattr(request.state, "request_id", str(uuid4())), created_at=now,
        ))
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        return JSONResponse({"error": "No se pudo generar el informe."}, status_code=500,
                            headers=_NO_STORE)
    pdf = render_report(data, begin=begin, end=end, generated_by=generated_by, generated_at=now)
    filename = f"auditoria-{tipo}-{now:%Y%m%d}.pdf"
    return Response(
        pdf, media_type="application/pdf",
        headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Content-Type-Options": "nosniff"},
    )


_GENERAL_TITLES = {
    "summary": "resumen",
    "couriers": "repartidores",
    "shipments": "envios",
    "scheduled": "programados",
}


@router.get("/informe-general")
async def download_fleet_report(
    request: Request,
    claims: Annotated[SessionClaims, Depends(require_dispatcher_session)],
    session: Annotated[AsyncSession, Depends(get_session)],
    tipo: Annotated[ReportKind, Query()],
    desde: Annotated[date, Query()],
    hasta: Annotated[date, Query()],
) -> Response:
    """PDF con datos de toda la operación en un periodo (no de un solo sujeto)."""
    account_raw = claims.get("accountId")
    requester = UUID(account_raw) if account_raw else None
    retry = _LIMITER.retry_after(hashlib.sha256((account_raw or "dispatcher").encode()).hexdigest())
    if retry is not None:
        return JSONResponse(
            {"error": "Demasiados informes seguidos. Intenta en unos segundos."},
            status_code=429, headers={**_NO_STORE, "Retry-After": str(retry)},
        )
    try:
        begin, end = period_bounds(desde, hasta)
    except AuditPeriodError as error:
        return JSONResponse({"error": str(error)}, status_code=400, headers=_NO_STORE)
    try:
        data = await FleetReportBuilder(session).build(tipo, begin, end)
        generated_by = "cuenta de despacho"
        if requester is not None:
            account = await session.get(Account, requester)
            generated_by = account.email if account else generated_by
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(SecurityAuditEvent(
            account_id=requester, action=f"audit_report:fleet_{tipo}", outcome="allowed",
            request_id=getattr(request.state, "request_id", str(uuid4())), created_at=now,
        ))
        await session.commit()
