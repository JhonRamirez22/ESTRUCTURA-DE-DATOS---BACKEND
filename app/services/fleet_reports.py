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
            (await self._session.execute(
                select(DeliveryEvent.event_type, func.count())
                .where(DeliveryEvent.occurred_at >= begin, DeliveryEvent.occurred_at < end)
                .group_by(DeliveryEvent.event_type)
            )).all()
        )
        routes = (
            await self._session.scalars(
                select(Route).where(Route.created_at >= begin, Route.created_at < end)
            )
        ).all()
        with_base = [r for r in routes if r.baseline_duration_minutes is not None]
        saved_min = sum(
            max(0.0, (r.baseline_duration_minutes or 0) - r.estimated_duration_minutes)
            for r in with_base if r.status != RouteStatus.CANCELLED
        )
        saved_km = sum(
            max(0.0, (r.baseline_distance_meters or 0) - r.estimated_distance_meters) / 1000
            for r in with_base if r.status != RouteStatus.CANCELLED
        )
        delivered = events.get(DeliveryEventType.DELIVERED.value, 0)
        failed = events.get(DeliveryEventType.FAILED.value, 0)
        rate = f"{100 * delivered / (delivered + failed):.0f} %" if delivered + failed else "—"
        data = ReportData(
            "Resumen de operación",
            [
                ("Envíos creados", str(sum(statuses.values()))),
                ("Entregados / fallidos", f"{delivered} / {failed}"),
                ("Tasa de éxito", rate),
                ("Rutas creadas", str(len(routes))),
                ("Ahorro por optimización", f"{saved_min:.0f} min · {saved_km:.1f} km"),
            ],
        )
        data.sections.append(Section(
            "Envíos creados por estado", ["Estado", "Envíos"],
            [[s.value, str(statuses.get(s, 0))] for s in DeliveryPointStatus], [4, 2],
        ))
        data.sections.append(Section(
            "Eventos del periodo", ["Evento", "Cantidad"],
            [[k, str(v)] for k, v in sorted(events.items(), key=lambda kv: -kv[1])], [4, 2],
        ))
        data.sections.append(Section(
            "Rutas del periodo", ["Creada", "Estado", "Est. min", "Base min", "Ahorro min"],
            [
                [local(r.created_at), r.status.value, _minutes(r.estimated_duration_minutes),
                 _minutes(r.baseline_duration_minutes),
                 _minutes(None if r.baseline_duration_minutes is None
                          else r.baseline_duration_minutes - r.estimated_duration_minutes)]
                for r in sorted(routes, key=lambda r: r.created_at, reverse=True)[:MAX_ROWS]
            ],
            [3, 3, 2, 2, 2],
            f"Se muestran {MAX_ROWS} de {len(routes)} rutas." if len(routes) > MAX_ROWS else None,
        ))
        return data

    # ---- courier performance ------------------------------------------------------------

    async def couriers(self, begin: datetime, end: datetime) -> ReportData:
        couriers = (await self._session.scalars(select(Courier).order_by(Courier.name))).all()
        by_courier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        rows = (await self._session.execute(
            select(DeliveryEvent.courier_id, DeliveryEvent.event_type, func.count())
            .where(DeliveryEvent.occurred_at >= begin, DeliveryEvent.occurred_at < end,
                   DeliveryEvent.courier_id.is_not(None))
            .group_by(DeliveryEvent.courier_id, DeliveryEvent.event_type)
        )).all()
        for courier_id, kind, count in rows:
            by_courier[str(courier_id)][kind] = int(count)
        routes = (await self._session.scalars(
            select(Route).where(Route.created_at >= begin, Route.created_at < end)
        )).all()
        per_route: dict[str, list[Route]] = defaultdict(list)
        for route in routes:
            per_route[str(route.courier_id)].append(route)
        table: list[list[str]] = []
        for courier in couriers:
            counts = by_courier.get(str(courier.id), {})
            mine = per_route.get(str(courier.id), [])
            delivered, failed = counts.get("DELIVERED", 0), counts.get("FAILED", 0)
            finished = [r for r in mine if r.started_at and r.completed_at
                        and r.status == RouteStatus.COMPLETED]
            actual = [(r.completed_at - r.started_at).total_seconds() / 60  # type: ignore[operator]
                      for r in finished]
            estimated = [r.estimated_duration_minutes for r in finished]
            km = await self._gps_km(courier.id, begin, end)
            table.append([
                courier.name, str(len(mine)), str(counts.get("ASSIGNED", 0)), str(delivered),
                str(failed),
                f"{100 * delivered / (delivered + failed):.0f} %" if delivered + failed else "—",
                _minutes(sum(actual) / len(actual) if actual else None),
                _minutes(sum(estimated) / len(estimated) if estimated else None),
                f"{km:.1f}",
            ])
        data = ReportData(
            "Rendimiento por repartidor",
            [("Repartidores", str(len(couriers))), ("Rutas del periodo", str(len(routes)))],
        )
        data.sections.append(Section(
            "Por repartidor",
            ["Repartidor", "Rutas", "Asig.", "Entreg.", "Fallid.", "Éxito", "Real min", "Est. min",
             "Km GPS"],
            table, [4, 2, 2, 2, 2, 2, 2, 2, 2],
        ))
        return data

    async def _gps_km(self, courier_id: object, begin: datetime, end: datetime) -> float:
        events = (await self._session.scalars(
            select(LocationEvent)
            .where(LocationEvent.courier_id == courier_id,
                   LocationEvent.recorded_at >= begin, LocationEvent.recorded_at < end)
            .order_by(LocationEvent.recorded_at.asc()).limit(20_000)
        )).all()
        total = 0.0
        for a, b in zip(events, events[1:], strict=False):
            step = haversine_m(a.lat, a.lng, b.lat, b.lng)
            if step <= GPS_GAP_BREAK_M:
                total += step
        return total / 1000.0

    # ---- shipment history ---------------------------------------------------------------

    async def shipments(self, begin: datetime, end: datetime) -> ReportData:
        total = int(await self._session.scalar(
            select(func.count()).select_from(DeliveryPoint)
            .where(DeliveryPoint.created_at >= begin, DeliveryPoint.created_at < end)) or 0)
        rows = (await self._session.execute(
            select(DeliveryPoint, Courier.name)
