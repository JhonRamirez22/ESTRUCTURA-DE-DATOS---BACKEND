"""Consultas y escrituras persistentes usadas al planificar rutas."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Courier,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteRevision,
    RouteStatus,
)


@dataclass(frozen=True, slots=True)
class RouteMetricSnapshot:
    status: RouteStatus
    baseline_duration_minutes: float | None
    estimated_duration_minutes: float
    baseline_distance_meters: float | None
    estimated_distance_meters: float


class RouteRepository:
    """Acceso a datos de ruta; la sesión y su transacción pertenecen al servicio."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_route(self, route: Route) -> None:
        self._session.add(route)

    async def get_courier(self, courier_id: UUID, *, for_update: bool = False) -> Courier | None:
        if not for_update:
            return await self._session.get(Courier, courier_id)
        return await self._session.scalar(
            select(Courier).where(Courier.id == courier_id).with_for_update()
        )

    async def get_delivery_points(self, point_ids: Sequence[UUID]) -> list[DeliveryPoint]:
        points = await self._session.scalars(
            select(DeliveryPoint).where(DeliveryPoint.id.in_(point_ids))
        )
        return list(points.all())

    async def list_routes(
        self,
        *,
        courier_id: UUID | None,
        status: RouteStatus | None,
    ) -> list[Route]:
        statement = select(Route).options(
            selectinload(Route.courier), selectinload(Route.delivery_points)
        )
        if courier_id is not None:
            statement = statement.where(Route.courier_id == courier_id)
        if status is not None:
            statement = statement.where(Route.status == status)
        routes = await self._session.scalars(statement.order_by(Route.created_at.desc()))
        return list(routes.all())

    async def get_route_courier_id(self, route_id: UUID) -> UUID | None:
        return await self._session.scalar(select(Route.courier_id).where(Route.id == route_id))

    async def get_route_record(self, route_id: UUID) -> Route | None:
        return await self._session.get(Route, route_id)

    async def list_location_events(
        self,
        *,
        courier_id: UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> list[LocationEvent]:
        events = await self._session.scalars(
            select(LocationEvent)
            .where(
                LocationEvent.courier_id == courier_id,
                LocationEvent.recorded_at >= start_at,
                LocationEvent.recorded_at <= end_at,
            )
            .order_by(LocationEvent.recorded_at.asc())
        )
        return list(events.all())

    async def list_metric_snapshots(
        self,
        courier_id: UUID | None,
    ) -> list[RouteMetricSnapshot]:
        statement = select(
            Route.status,
            Route.baseline_duration_minutes,
            Route.estimated_duration_minutes,
            Route.baseline_distance_meters,
            Route.estimated_distance_meters,
        ).where(Route.status != RouteStatus.CANCELLED)
        if courier_id is not None:
            statement = statement.where(Route.courier_id == courier_id)
        rows = await self._session.execute(statement)
        return [
            RouteMetricSnapshot(
                status=row.status,
                baseline_duration_minutes=row.baseline_duration_minutes,
                estimated_duration_minutes=row.estimated_duration_minutes,
                baseline_distance_meters=row.baseline_distance_meters,
                estimated_distance_meters=row.estimated_distance_meters,
            )
            for row in rows
        ]

    async def lock_couriers(self, courier_ids: Sequence[UUID]) -> dict[UUID, Courier]:
        couriers = await self._session.scalars(
            select(Courier)
            .where(Courier.id.in_(courier_ids))
            .order_by(Courier.id.asc())
            .with_for_update()
        )
        return {courier.id: courier for courier in couriers}

    async def has_active_route(
        self,
        courier_ids: Sequence[UUID],
        active_statuses: Sequence[RouteStatus],
    ) -> bool:
        active_courier_id = await self._session.scalar(
            select(Route.courier_id).where(
                Route.courier_id.in_(courier_ids),
                Route.status.in_(active_statuses),
            )
        )
        return active_courier_id is not None

    async def lock_route(self, route_id: UUID) -> Route | None:
        return await self._session.scalar(
            select(Route).where(Route.id == route_id).with_for_update()
        )
