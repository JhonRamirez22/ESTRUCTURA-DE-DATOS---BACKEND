"""Limpieza transaccional de fixtures de Playwright, sin endpoints de producción."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Account,
    Courier,
    CourierCredential,
    DeliveryPoint,
    LocationEvent,
    Notification,
    Route,
)
from app.settings import get_settings


def _parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--courier-id", action="append", type=UUID, default=[])
    parser.add_argument("--delivery-point-id", action="append", type=UUID, default=[])
    parser.add_argument("--order-id", action="append", default=[])
    parsed = parser.parse_args(arguments)
    if not parsed.courier_id and not parsed.delivery_point_id and not parsed.order_id:
        parser.error("se debe indicar al menos un identificador de fixture")
    return parsed


async def _cleanup(
    *,
    courier_ids: Sequence[UUID],
    delivery_point_ids: Sequence[UUID],
    order_ids: Sequence[str],
) -> None:
    engine = create_async_engine(get_settings().sqlalchemy_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session, session.begin():
            point_filters = []
            if courier_ids:
                route_ids = select(Route.id).where(Route.courier_id.in_(courier_ids))
                point_filters.append(DeliveryPoint.route_id.in_(route_ids))
            if delivery_point_ids:
                point_filters.append(DeliveryPoint.id.in_(delivery_point_ids))
            if order_ids:
                point_filters.append(DeliveryPoint.order_id.in_(order_ids))

            point_filter = or_(*point_filters)
            related_order_ids = list(
                (await session.scalars(select(DeliveryPoint.order_id).where(point_filter))).all()
            )

            notification_filters = [
                Notification.message.contains(order_id) for order_id in related_order_ids
            ]
            if courier_ids:
                notification_filters.append(
                    Notification.recipient_id.in_([str(courier_id) for courier_id in courier_ids])
                )
            if notification_filters:
                await session.execute(delete(Notification).where(or_(*notification_filters)))

            await session.execute(delete(DeliveryPoint).where(point_filter))
            if courier_ids:
                await session.execute(delete(Account).where(Account.courier_id.in_(courier_ids)))
                await session.execute(delete(Route).where(Route.courier_id.in_(courier_ids)))
                await session.execute(
                    delete(LocationEvent).where(LocationEvent.courier_id.in_(courier_ids))
                )
                await session.execute(
                    delete(CourierCredential).where(CourierCredential.courier_id.in_(courier_ids))
                )
                await session.execute(delete(Courier).where(Courier.id.in_(courier_ids)))
    finally:
        await engine.dispose()


async def main() -> int:
    parsed = _parse_arguments()
    try:
        await _cleanup(
            courier_ids=parsed.courier_id,
            delivery_point_ids=parsed.delivery_point_id,
            order_ids=parsed.order_id,
        )
    except Exception:
        print("No se pudieron limpiar los datos de prueba e2e.", file=sys.stderr)
        return 1

    print("Limpieza e2e completa.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
