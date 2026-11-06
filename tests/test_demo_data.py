from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import Account, Courier, DeliveryPoint
from app.services.demo_data import (
    DEMO_COURIERS,
    DEMO_ORDERS,
    count_operational_data,
    reset_operational_data,
    seed_demo,
)

NOW = datetime(2026, 10, 20, 12, 0, 0)


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'demo.sqlite'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_creates_no_customer_contacts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        first = await seed_demo(session, now=NOW)
        second = await seed_demo(session, now=NOW)
        assert first.orders_created == len(DEMO_ORDERS) and second.orders_created == 0
        assert await session.scalar(select(func.count()).select_from(Courier)) == len(
            DEMO_COURIERS
        )
        points = list(await session.scalars(select(DeliveryPoint)))
        assert all(
            point.customer_email is None and point.customer_phone is None for point in points
        )
        assert not any(point.email_notifications_enabled for point in points)


@pytest.mark.asyncio
async def test_seed_accounts_require_passwords_from_the_environment(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await seed_demo(session, now=NOW)
        assert await session.scalar(select(func.count()).select_from(Account)) == 0
        with pytest.raises(ValueError):
            await seed_demo(session, now=NOW, dispatcher_password="corta")
        summary = await seed_demo(
            session, now=NOW, dispatcher_password="x" * 14, courier_password="y" * 14,
        )
        assert summary.accounts == 2


@pytest.mark.asyncio
async def test_reset_removes_operational_data_but_keeps_accounts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        await seed_demo(session, now=NOW, dispatcher_password="x" * 14)
        before = await count_operational_data(session)
        assert before["delivery_points"] == len(DEMO_ORDERS)
        removed = await reset_operational_data(session)
        assert removed == before
        after = await count_operational_data(session)
        assert set(after.values()) == {0}
        assert await session.scalar(select(func.count()).select_from(Account)) == 1
