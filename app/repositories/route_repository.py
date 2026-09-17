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
    TrafficObservation,
)
from app.repositories.limits import MAX_ROUTE_ROWS


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
        routes = await self._session.scalars(
            statement.order_by(Route.created_at.desc()).limit(MAX_ROUTE_ROWS)
        )
        return list(routes.all())

    async def list_recent_traffic_observations(
        self, since: datetime, limit: int = 5_000
