"""Informes de auditoría por usuario: qué hizo, cuándo y con qué resultado (solo despacho).

Recoge de la base de datos los movimientos de un repartidor, una cuenta de despacho o un pedido
(cliente) en un periodo y los entrega como ``ReportData`` neutro; ``render_report`` lo convierte
en PDF. Los datos de contacto del cliente se muestran enmascarados: la auditoría prueba que
hubo avisos, no reexpone el teléfono ni el correo completos.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.location.proximity import haversine_m
from app.core.reports.pdf import PdfReport
from app.models import (
    Courier,
    CustomerNotificationOutbox,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Notification,
    Route,
)
from app.models.account import Account
from app.models.security import AccessSession, SecurityAuditEvent

BOGOTA = timedelta(hours=-5)
MAX_ROWS = 300
MAX_DAYS = 366
GPS_GAP_BREAK_M = 2_000.0  # un salto mayor no cuenta como recorrido (GPS perdido)


@dataclass(slots=True)
class Section:
    title: str
    headers: list[str]
    rows: list[list[str]]
    fractions: list[float]
    note: str | None = None


@dataclass(slots=True)
class ReportData:
    title: str
    subject: list[tuple[str, str]]
    sections: list[Section] = field(default_factory=list)


class AuditPeriodError(ValueError):
    """El periodo pedido no es válido."""


def period_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Fechas locales de Bogotá a instantes UTC [inicio, fin)."""
    if end < start:
        raise AuditPeriodError("La fecha final debe ser igual o posterior a la inicial.")
    if (end - start).days >= MAX_DAYS:
        raise AuditPeriodError(f"El periodo no puede superar {MAX_DAYS} días.")
    begin = datetime(start.year, start.month, start.day) - BOGOTA
    finish = datetime(end.year, end.month, end.day) + timedelta(days=1) - BOGOTA
    return begin, finish


def local(moment: datetime | None) -> str:
    return "—" if moment is None else (moment + BOGOTA).strftime("%Y-%m-%d %H:%M")


def mask_email(value: str | None) -> str:
    if not value or "@" not in value:
        return "—"
    name, domain = value.split("@", 1)
    return f"{name[:1]}***@{domain}"


def mask_phone(value: str | None) -> str:
    if not value:
        return "—"
    return f"***{value[-3:]}" if len(value) > 3 else "***"


def _cap(rows: list[list[str]], total: int) -> tuple[list[list[str]], str | None]:
    if total <= MAX_ROWS:
        return rows, None
    return rows[:MAX_ROWS], f"Se muestran {MAX_ROWS} de {total} registros (los más recientes)."


def _distance_km(events: Sequence[LocationEvent]) -> float:
    total = 0.0
    for before, after in zip(events, events[1:], strict=False):
        step = haversine_m(before.lat, before.lng, after.lat, after.lng)
        if step <= GPS_GAP_BREAK_M:
            total += step
    return total / 1000.0


async def _security_section(
    session: AsyncSession, account_id: UUID, begin: datetime, end: datetime
) -> Section:
    rows = (
        await session.scalars(
            select(SecurityAuditEvent)
            .where(
                SecurityAuditEvent.account_id == account_id,
                SecurityAuditEvent.created_at >= begin,
                SecurityAuditEvent.created_at < end,
            )
            .order_by(SecurityAuditEvent.created_at.desc())
            .limit(MAX_ROWS + 1)
        )
    ).all()
    total = len(rows)
    table = [
        [local(e.created_at), e.action, e.outcome, e.request_id[:8]] for e in rows[:MAX_ROWS]
    ]
    note = f"Se muestran los {MAX_ROWS} más recientes." if total > MAX_ROWS else None
    return Section(
        "Eventos de seguridad", ["Fecha (Bogotá)", "Acción", "Resultado", "Solicitud"],
        table, [3, 4, 2, 2], note,
    )


async def _sessions_section(
    session: AsyncSession, account_id: UUID, begin: datetime, end: datetime
) -> Section:
    rows = (
        await session.scalars(
            select(AccessSession)
            .where(
                AccessSession.account_id == account_id,
                AccessSession.created_at >= begin,
                AccessSession.created_at < end,
            )
            .order_by(AccessSession.created_at.desc())
            .limit(MAX_ROWS)
        )
    ).all()
    table = [
        [local(s.created_at), local(s.last_seen_at), local(s.expires_at),
         "Revocada " + local(s.revoked_at) if s.revoked_at else "—"]
        for s in rows
    ]
    return Section(
        "Sesiones de acceso", ["Inicio", "Última actividad", "Vence", "Revocación"],
        table, [3, 3, 3, 3],
    )


async def collect_courier_report(
    session: AsyncSession, courier_id: UUID, begin: datetime, end: datetime
) -> ReportData | None:
    courier = await session.get(Courier, courier_id)
    if courier is None:
        return None
    account = await session.scalar(select(Account).where(Account.courier_id == courier_id))
    events = (
        await session.scalars(
            select(LocationEvent)
            .where(
                LocationEvent.courier_id == courier_id,
                LocationEvent.recorded_at >= begin,
                LocationEvent.recorded_at < end,
