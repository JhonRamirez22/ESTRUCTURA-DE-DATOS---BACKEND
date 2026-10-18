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

    smtp_client = MagicMock()
    smtp_factory = MagicMock()
    smtp_factory.return_value.__enter__.return_value = smtp_client
    monkeypatch.setattr(notifications.smtplib, factory_name, smtp_factory)
    settings = Settings(
        notification_smtp_host="smtp.example.com",
        notification_smtp_port=port,
        notification_smtp_username="mailer",
        notification_smtp_password=SecretStr("test-password"),
        notification_from_email="notificaciones@example.com",
    )

    SmtpEmailSender(settings).send(
        CustomerEmail(
            recipient="cliente@example.com",
            subject="Tu pedido fue asignado",
            text_body="La ruta ya fue creada.",
        )
    )

    smtp_factory.assert_called_once()
    assert smtp_factory.call_args.args == ("smtp.example.com", port)
    smtp_client.login.assert_called_once_with("mailer", "test-password")
    assert smtp_client.starttls.called is (port == 587)
    smtp_message = smtp_client.send_message.call_args.args[0]
    assert smtp_message["To"] == "cliente@example.com"
    assert smtp_message["From"] == "notificaciones@example.com"
    assert smtp_message["Subject"] == "Tu pedido fue asignado"
    assert smtp_message.get_content().strip() == "La ruta ya fue creada."


async def _create_point(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    opted_in: bool = True,
    sms_opted_in: bool = False,
) -> DeliveryPoint:
    point = DeliveryPoint(
        id=uuid4(),
        address="Carrera 25 # 4 sur 65, Pasto",
        lat=1.214,
        lng=-77.2811,
        status=DeliveryPointStatus.PENDING,
        order_id=f"PEDIDO-NOTIFICACION-{uuid4()}",
        customer_email="cliente@example.com",
        email_notifications_enabled=opted_in,
        customer_phone="+573001234567" if sms_opted_in else None,
        sms_notifications_enabled=sms_opted_in,
    )
    async with session_factory() as session:
        session.add(point)
        await session.commit()
        await session.refresh(point)
    return point


@pytest.mark.asyncio
async def test_events_require_explicit_email_consent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    opted_out = await _create_point(session_factory, opted_in=False)
    opted_in = await _create_point(session_factory)
    route_id = uuid4()

    async with session_factory() as session:
        first = await session.get(DeliveryPoint, opted_out.id)
        second = await session.get(DeliveryPoint, opted_in.id)
        assert first is not None and second is not None
        assert await enqueue_order_assigned_notification(session, first, route_id) == ()
        assert await enqueue_order_assigned_notification(session, second, route_id) == (
            CustomerNotificationChannel.EMAIL,
        )
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.scalars(select(CustomerNotificationOutbox))).all())
        assert len(rows) == 1
        assert rows[0].delivery_point_id == opted_in.id


@pytest.mark.asyncio
async def test_assignment_and_nearby_events_are_idempotent_and_distinct(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory)
    route_id = uuid4()

    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        assert await enqueue_order_assigned_notification(session, persisted, route_id) == (
            CustomerNotificationChannel.EMAIL,
        )
        assert await enqueue_order_assigned_notification(session, persisted, route_id) == ()
        assert await enqueue_courier_near_notification(session, persisted, route_id) == (
            CustomerNotificationChannel.EMAIL,
        )
        assert await enqueue_courier_near_notification(session, persisted, route_id) == ()
        await session.commit()

    async with session_factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(CustomerNotificationOutbox).order_by(
                        CustomerNotificationOutbox.event_type
                    )
                )
            ).all()
        )
        assert {row.event_type for row in rows} == {
            CustomerNotificationEvent.ORDER_ASSIGNED,
            CustomerNotificationEvent.COURIER_NEAR,
        }
        assert len({row.event_key for row in rows}) == 2
