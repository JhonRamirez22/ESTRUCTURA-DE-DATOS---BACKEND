from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models import (
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
    DeliveryPointStatus,
)
from app.services.customer_notifications import (
    MAX_NOTIFICATION_ATTEMPTS,
    CustomerEmail,
    CustomerSms,
    SmtpEmailSender,
    TwilioSmsSender,
    cancel_pending_customer_notifications,
    dispatch_pending_customer_notifications,
    enqueue_courier_near_notification,
    enqueue_order_assigned_notification,
    is_courier_near_delivery_point,
)
from app.settings import Settings


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "customer-notifications.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(connection: Connection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeSender:
    def __init__(self, failure: Exception | None = None) -> None:
        self.messages: list[CustomerEmail] = []
        self.failure = failure

    def send(self, message: CustomerEmail) -> None:
        if self.failure is not None:
            raise self.failure
        self.messages.append(message)


def _smtp_settings() -> Settings:
    return Settings(
        notification_smtp_host="smtp.example.com",
        notification_from_email="notificaciones@example.com",
    )


@pytest.mark.parametrize(("port", "factory_name"), [(587, "SMTP"), (465, "SMTP_SSL")])
def test_smtp_sender_uses_encrypted_transport_and_authentication(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    factory_name: str,
) -> None:
    import app.services.customer_notifications as notifications

