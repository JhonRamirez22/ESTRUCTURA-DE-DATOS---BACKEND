"""Persistencia de repartidores, credenciales y posiciones GPS."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Courier,
    CourierCredential,
    CourierStatus,
    DeliveryPoint,
    DeliveryPointStatus,
    LocationEvent,
    Route,
    RouteStatus,
)
from app.repositories.limits import MAX_LIST_ROWS


class CourierRepository:
    """Acceso persistente; el llamador conserva el control de la transacción."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_courier(self, courier_id: UUID) -> Courier | None:
        return await self._session.get(Courier, courier_id)

    async def list_couriers(self, status: CourierStatus | None = None) -> list[Courier]:
        statement = select(Courier)
        if status is not None:
            statement = statement.where(Courier.status == status)
        couriers = await self._session.scalars(
            statement.order_by(Courier.name.asc(), Courier.id.asc()).limit(MAX_LIST_ROWS)
        )
        return list(couriers.all())

    async def list_available_for_assignment(
        self,
        *,
        fresh_after: datetime,
        as_of: datetime,
        courier_ids: Sequence[UUID] | None,
    ) -> list[Courier]:
