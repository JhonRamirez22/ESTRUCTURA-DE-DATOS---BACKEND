"""History anonymization: only old finished shipments lose personal data."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import DeliveryPoint, DeliveryPointStatus
from app.services.access_sessions import utc_now
from scripts.anonymize_history import ANONYMIZED_ADDRESS, HistoryAnonymizer


@pytest_asyncio.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'anon.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _point(order: str, status: DeliveryPointStatus, age_days: int) -> DeliveryPoint:
    return DeliveryPoint(
        id=uuid4(), address="Calle 18 # 20-30", lat=1.2, lng=-77.28, status=status,
        order_id=order, tracking_token=f"t-{order}", customer_email="a@b.co",
        customer_phone="3001234567", created_at=utc_now() - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_only_old_finished_shipments_are_anonymized(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        session.add_all([
            _point("OLD-DONE", DeliveryPointStatus.DELIVERED, 400),
            _point("OLD-PENDING", DeliveryPointStatus.PENDING, 400),
            _point("NEW-DONE", DeliveryPointStatus.DELIVERED, 10),
        ])
        await session.commit()
    async with factory() as session:
        worker = HistoryAnonymizer(session, months=12)
        assert await worker.count() == 1  # simulation changes nothing
        assert await worker.apply() == 1
        assert await worker.count() == 0  # idempotent
    async with factory() as session:
        rows = {p.order_id: p for p in (await session.scalars(select(DeliveryPoint))).all()}
    assert rows["OLD-DONE"].address == ANONYMIZED_ADDRESS
    assert rows["OLD-DONE"].customer_email is None and rows["OLD-DONE"].customer_phone is None
    assert rows["OLD-PENDING"].customer_email == "a@b.co"
    assert rows["NEW-DONE"].address == "Calle 18 # 20-30"


def test_months_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HistoryAnonymizer(None, months=0)  # type: ignore[arg-type]
