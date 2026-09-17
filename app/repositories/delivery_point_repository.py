"""Persistencia de pedidos/puntos de entrega sin administrar transacciones."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import DeliveryPoint, DeliveryPointStatus, Route, RouteStatus
from app.repositories.limits import MAX_LIST_ROWS


class DeliveryPointRepository:
    """Operaciones de almacenamiento; el endpoint controla el commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_delivery_points(
        self, status: DeliveryPointStatus | None = None
    ) -> list[DeliveryPoint]:
        statement = select(DeliveryPoint)
        if status is not None:
            statement = statement.where(DeliveryPoint.status == status)
        points = await self._session.scalars(
            statement.order_by(DeliveryPoint.id.asc()).limit(MAX_LIST_ROWS)
        )
        return list(points.all())

    async def list_pending_unassigned(
        self,
        point_ids: Sequence[UUID] | None = None,
    ) -> list[DeliveryPoint]:
        statement = select(DeliveryPoint).where(
            DeliveryPoint.status == DeliveryPointStatus.PENDING,
            DeliveryPoint.route_id.is_(None),
        )
        if point_ids is not None:
            statement = statement.where(DeliveryPoint.id.in_(point_ids))
        points = await self._session.scalars(statement.order_by(DeliveryPoint.id.asc()))
        return list(points.all())

    async def get_delivery_point(
        self,
        point_id: UUID,
        *,
        include_route: bool = False,
    ) -> DeliveryPoint | None:
        statement = select(DeliveryPoint).where(DeliveryPoint.id == point_id)
        if include_route:
            statement = statement.options(selectinload(DeliveryPoint.route))
        return await self._session.scalar(statement)

