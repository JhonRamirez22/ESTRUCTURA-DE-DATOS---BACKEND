"""Fleet-wide report builders: period summary, courier performance, history and schedules.

Each builder returns a neutral :class:`ReportData`; ``render_report`` turns it into a PDF.
Tables are capped so a huge period never produces an unreadable file.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.location.proximity import haversine_m
from app.models import (
    Courier,
    DeliveryEvent,
    DeliveryEventType,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteStatus,
)
from app.services.access_sessions import utc_now
from app.services.audit_report import BOGOTA, MAX_ROWS, ReportData, Section, local

ReportKind = Literal["summary", "couriers", "shipments", "scheduled"]
ON_TIME_GRACE = timedelta(minutes=15)
GPS_GAP_BREAK_M = 2_000.0


def _minutes(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


class FleetReportBuilder:
    """Builds the four general reports for a period (UTC bounds, end exclusive)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(self, kind: ReportKind, begin: datetime, end: datetime) -> ReportData:
        builders = {
            "summary": self.summary,
            "couriers": self.couriers,
            "shipments": self.shipments,
            "scheduled": self.scheduled,
        }
        return await builders[kind](begin, end)

    # ---- period summary -----------------------------------------------------------------

    async def summary(self, begin: datetime, end: datetime) -> ReportData:
        statuses = dict(
            (await self._session.execute(
                select(DeliveryPoint.status, func.count())
                .where(DeliveryPoint.created_at >= begin, DeliveryPoint.created_at < end)
                .group_by(DeliveryPoint.status)
            )).all()
        )
        events = dict(
