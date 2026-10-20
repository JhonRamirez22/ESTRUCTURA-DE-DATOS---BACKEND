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
        statement = select(Courier).where(
            Courier.status == CourierStatus.AVAILABLE,
            Courier.current_lat.is_not(None),
            Courier.current_lng.is_not(None),
            Courier.last_location_at >= fresh_after,
            Courier.last_location_at <= as_of,
        )
        if courier_ids is not None:
            statement = statement.where(Courier.id.in_(courier_ids))
        couriers = await self._session.scalars(statement.order_by(Courier.id.asc()))
        return list(couriers.all())

    async def has_routes(self, courier_id: UUID) -> bool:
        route_id = await self._session.scalar(
            select(Route.id).where(Route.courier_id == courier_id).limit(1)
        )
        return route_id is not None

    async def get_credential(self, courier_id: UUID) -> CourierCredential | None:
        return await self._session.get(CourierCredential, courier_id)

    def add_courier(self, courier: Courier) -> None:
        self._session.add(courier)

    async def refresh_courier(self, courier: Courier) -> None:
        await self._session.refresh(courier)

    def add_credential(self, credential: CourierCredential) -> None:
        self._session.add(credential)

    async def delete_courier(self, courier: Courier) -> None:
        await self._session.delete(courier)

    async def get_active_route_id(self, courier_id: UUID) -> UUID | None:
        return await self._session.scalar(
            select(Route.id)
            .where(
                Route.courier_id == courier_id,
                Route.status.in_([RouteStatus.PLANNED, RouteStatus.IN_PROGRESS]),
            )
            .limit(1)
        )

    async def get_next_delivery_point_for_route(self, courier_id: UUID) -> DeliveryPoint | None:
        return await self._session.scalar(
            select(DeliveryPoint)
            .join(Route, Route.id == DeliveryPoint.route_id)
            .where(
                Route.courier_id == courier_id,
                Route.status == RouteStatus.IN_PROGRESS,
                DeliveryPoint.status == DeliveryPointStatus.EN_ROUTE,
            )
            .order_by(DeliveryPoint.sequence_index.asc())
            .limit(1)
        )

    async def courier_exists(self, courier_id: UUID) -> bool:
        existing_id = await self._session.scalar(
            select(Courier.id).where(Courier.id == courier_id)
        )
        return existing_id is not None

    async def record_location(
        self,
        *,
        courier_id: UUID,
        latitude: float,
        longitude: float,
        recorded_at: datetime,
    ) -> tuple[LocationEvent, bool]:
        event = LocationEvent(
            courier_id=courier_id,
            lat=latitude,
            lng=longitude,
            recorded_at=recorded_at,
        )
        self._session.add(event)
        active_route_id = await self.get_active_route_id(courier_id)
        updated_id = await self._session.scalar(
            update(Courier)
            .where(
                Courier.id == courier_id,
                or_(Courier.last_location_at.is_(None), Courier.last_location_at < recorded_at),
            )
            .values(
                current_lat=latitude,
                current_lng=longitude,
                last_location_at=recorded_at,
                status=CourierStatus.ON_ROUTE
                if active_route_id is not None
                else CourierStatus.AVAILABLE,
            )
            .returning(Courier.id)
        )
        await self._session.flush()
        return event, updated_id == courier_id
