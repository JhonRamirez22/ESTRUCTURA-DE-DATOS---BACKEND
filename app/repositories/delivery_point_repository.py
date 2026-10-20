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

    async def get_delivery_point_by_tracking_token(self, token: str) -> DeliveryPoint | None:
        statement = (
            select(DeliveryPoint)
            .where(DeliveryPoint.tracking_token == token)
            .options(
                selectinload(DeliveryPoint.route).joinedload(Route.courier),
                selectinload(DeliveryPoint.route).selectinload(Route.delivery_points),
            )
        )
        return await self._session.scalar(statement)

    def add(self, point: DeliveryPoint) -> None:
        self._session.add(point)

    async def refresh(self, point: DeliveryPoint) -> None:
        await self._session.refresh(point)

    async def try_update_delivery_point(
        self,
        *,
        point_id: UUID,
        expected_status: DeliveryPointStatus,
        expected_route_id: UUID | None,
        updates: Mapping[str, object],
        courier_id: UUID | None = None,
    ) -> bool:
        conditions = [
            DeliveryPoint.id == point_id,
            DeliveryPoint.status == expected_status,
            DeliveryPoint.route_id == expected_route_id,
        ]
        if courier_id is not None:
            conditions.append(
                DeliveryPoint.route_id.in_(
                    select(Route.id).where(
                        Route.courier_id == courier_id,
                        Route.status == RouteStatus.IN_PROGRESS,
                    )
                )
            )
        result = await self._session.execute(
            update(DeliveryPoint)
            .where(*conditions)
            .values(**updates)
            .returning(DeliveryPoint.id)
        )
        return result.scalar_one_or_none() == point_id

    async def count_requested(self) -> int:
        """Solicitudes de clientes en espera; acota cuántas puede acumular la bandeja."""
        total = await self._session.scalar(
            select(func.count())
            .select_from(DeliveryPoint)
            .where(DeliveryPoint.status == DeliveryPointStatus.REQUESTED)
        )
        return int(total or 0)

    async def accept_request(self, point_id: UUID) -> bool:
        """Pasa una solicitud a pedido pendiente; falla si otro despachador ya la resolvió."""
        result = await self._session.execute(
            update(DeliveryPoint)
            .where(
                DeliveryPoint.id == point_id,
                DeliveryPoint.status == DeliveryPointStatus.REQUESTED,
                DeliveryPoint.route_id.is_(None),
            )
            .values(status=DeliveryPointStatus.PENDING)
            .returning(DeliveryPoint.id)
        )
        return result.scalar_one_or_none() == point_id

    async def reject_request(self, point_id: UUID) -> bool:
        """Descarta una solicitud no aceptada: nunca entró a la operación."""
        result = await self._session.execute(
            delete(DeliveryPoint)
            .where(
                DeliveryPoint.id == point_id,
                DeliveryPoint.status == DeliveryPointStatus.REQUESTED,
                DeliveryPoint.route_id.is_(None),
            )
            .returning(DeliveryPoint.id)
        )
        return result.scalar_one_or_none() == point_id

    async def delete_if_pending_unassigned(self, point_id: UUID) -> bool:
        result = await self._session.execute(
            delete(DeliveryPoint)
            .where(
                DeliveryPoint.id == point_id,
                DeliveryPoint.status == DeliveryPointStatus.PENDING,
                DeliveryPoint.route_id.is_(None),
            )
            .returning(DeliveryPoint.id)
        )
        return result.scalar_one_or_none() == point_id
