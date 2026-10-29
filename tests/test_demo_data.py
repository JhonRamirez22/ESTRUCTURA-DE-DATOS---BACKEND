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
