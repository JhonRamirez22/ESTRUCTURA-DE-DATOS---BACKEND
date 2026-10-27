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
