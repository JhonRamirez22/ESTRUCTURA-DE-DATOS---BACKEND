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

    async def lock_route_with_delivery_points(self, route_id: UUID) -> Route | None:
        return await self._session.scalar(
            select(Route)
            .where(Route.id == route_id)
            .options(selectinload(Route.delivery_points))
            .with_for_update()
        )

    async def lock_route_delivery_points(self, route_id: UUID) -> list[DeliveryPoint]:
        points = await self._session.scalars(
            select(DeliveryPoint)
            .where(DeliveryPoint.route_id == route_id)
            .with_for_update()
        )
        return list(points.all())

    async def release_active_delivery_points(
        self,
        route_id: UUID,
        active_statuses: Sequence[DeliveryPointStatus],
        *,
        reset_to_pending: bool,
    ) -> None:
        if reset_to_pending:
            await self._session.execute(
                update(DeliveryPoint)
                .where(
                    DeliveryPoint.route_id == route_id,
                    DeliveryPoint.status.in_(active_statuses),
                )
                .values(
                    route_id=None,
                    sequence_index=None,
                    status=DeliveryPointStatus.PENDING,
                )
            )
            return
        await self._session.execute(
            update(DeliveryPoint)
            .where(
                DeliveryPoint.route_id == route_id,
                DeliveryPoint.status.in_(active_statuses),
            )
            .values(route_id=None, sequence_index=None)
        )

    async def mark_pending_delivery_points_en_route(self, route_id: UUID) -> None:
        await self._session.execute(
            update(DeliveryPoint)
            .where(
                DeliveryPoint.route_id == route_id,
                DeliveryPoint.status == DeliveryPointStatus.PENDING,
            )
            .values(status=DeliveryPointStatus.EN_ROUTE)
        )

    async def try_assign_delivery_point(
        self,
        *,
        route_id: UUID,
        point_id: UUID,
        allowed_statuses: Sequence[DeliveryPointStatus],
        expected_latitude: float,
        expected_longitude: float,
        sequence_index: int,
    ) -> bool:
        updated_id = await self._session.scalar(
            update(DeliveryPoint)
            .where(
                DeliveryPoint.id == point_id,
                DeliveryPoint.route_id.is_(None),
                DeliveryPoint.status.in_(allowed_statuses),
                DeliveryPoint.lat == expected_latitude,
                DeliveryPoint.lng == expected_longitude,
            )
            .values(route_id=route_id, sequence_index=sequence_index)
            .returning(DeliveryPoint.id)
        )
        return updated_id == point_id

    async def load_route(self, route_id: UUID) -> Route | None:
        return await self._session.scalar(
            select(Route)
            .where(Route.id == route_id)
            .options(selectinload(Route.courier), selectinload(Route.delivery_points))
            .execution_options(populate_existing=True)
        )

    async def list_route_revisions(self, route_id: UUID) -> list[RouteRevision]:
        revisions = await self._session.scalars(
            select(RouteRevision)
            .where(RouteRevision.route_id == route_id)
            .order_by(RouteRevision.revision_number.asc())
        )
        return list(revisions.all())

    async def route_ids_with_revisions(self, route_ids: Sequence[UUID]) -> set[UUID]:
        if not route_ids:
            return set()
        revision_route_ids = await self._session.scalars(
            select(RouteRevision.route_id)
            .where(RouteRevision.route_id.in_(route_ids))
            .distinct()
        )
        return set(revision_route_ids.all())

    async def next_route_revision_number(self, route_id: UUID) -> int:
        latest_number = await self._session.scalar(
            select(func.max(RouteRevision.revision_number)).where(
                RouteRevision.route_id == route_id
            )
        )
        return (latest_number or 0) + 1

    def add_route_revision(self, revision: RouteRevision) -> None:
        self._session.add(revision)

    async def prune_route_revisions(self, route_id: UUID, keep: int) -> None:
        revision_ids = list(
            (
                await self._session.scalars(
                    select(RouteRevision.id)
                    .where(RouteRevision.route_id == route_id)
                    .order_by(RouteRevision.revision_number.desc())
                    .limit(keep)
                )
            ).all()
        )
        await self._session.execute(
            delete(RouteRevision).where(
                RouteRevision.route_id == route_id,
                RouteRevision.id.not_in(revision_ids),
            )
        )

    async def delete_route_revision(self, revision_id: UUID) -> None:
        await self._session.execute(
            delete(RouteRevision).where(RouteRevision.id == revision_id)
        )

    async def delete_route_revisions(self, route_id: UUID) -> None:
        await self._session.execute(
            delete(RouteRevision).where(RouteRevision.route_id == route_id)
        )
