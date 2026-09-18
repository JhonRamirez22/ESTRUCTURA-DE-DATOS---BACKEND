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
