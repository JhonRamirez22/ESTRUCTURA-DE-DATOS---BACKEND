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
        assert all("cliente@example.com" not in row.text_body for row in rows)
        assert all("Carrera 25" not in row.text_body for row in rows)


@pytest.mark.asyncio
async def test_customer_messages_never_include_internal_order_reference(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory, sms_opted_in=True)
    route_id = uuid4()

    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_order_assigned_notification(session, persisted, route_id)
        await enqueue_courier_near_notification(session, persisted, route_id)
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.scalars(select(CustomerNotificationOutbox))).all())

    assert len(rows) == 4
    for row in rows:
        assert point.order_id not in row.text_body
        assert "cliente@example.com" not in row.text_body
        assert "+573001234567" not in row.text_body


@pytest.mark.asyncio
async def test_sms_is_queued_only_after_explicit_opt_in_and_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    opted_out = await _create_point(session_factory, opted_in=False)
    opted_in = await _create_point(session_factory, opted_in=False, sms_opted_in=True)
    route_id = uuid4()

    async with session_factory() as session:
        first = await session.get(DeliveryPoint, opted_out.id)
        second = await session.get(DeliveryPoint, opted_in.id)
        assert first is not None and second is not None
        assert await enqueue_order_assigned_notification(session, first, route_id) == ()
        assert await enqueue_order_assigned_notification(session, second, route_id) == (
            CustomerNotificationChannel.SMS,
        )
        assert await enqueue_order_assigned_notification(session, second, route_id) == ()
        await session.commit()

    async with session_factory() as session:
        rows = list((await session.scalars(select(CustomerNotificationOutbox))).all())
        assert len(rows) == 1
        assert rows[0].channel is CustomerNotificationChannel.SMS
        assert rows[0].recipient_email is None
        assert rows[0].recipient_phone == "+573001234567"
        assert "3001234567" not in rows[0].text_body


def test_twilio_sender_posts_opted_in_phone_over_https_without_putting_credentials_in_url() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"sid": "SM-test"})

    settings = Settings(
        notification_sms_twilio_account_sid="AC" + "a" * 32,
        notification_sms_twilio_auth_token=SecretStr("private-token"),
        notification_sms_twilio_from_phone="+15005550006",
    )
    TwilioSmsSender(settings, transport=httpx.MockTransport(respond)).send(
        CustomerSms(recipient="+573001234567", text_body="Tu pedido fue asignado.")
    )

    assert captured["url"] == (
        "https://api.twilio.com/2010-04-01/Accounts/AC" + "a" * 32 + "/Messages.json"
    )
    assert "private-token" not in str(captured["url"])
    assert str(captured["authorization"]).startswith("Basic ")
    assert "To=%2B573001234567" in str(captured["body"])
    assert "Body=Tu+pedido+fue+asignado." in str(captured["body"])


def test_proximity_is_a_bounded_distance_estimate_not_an_eta() -> None:
    assert is_courier_near_delivery_point(1.2136, -77.2811, 1.214, -77.2811)
    assert not is_courier_near_delivery_point(1.2136, -77.2811, 1.22, -77.2811)
    assert not is_courier_near_delivery_point(1.2136, -77.2811, math.nan, -77.2811)


@pytest.mark.asyncio
async def test_successful_delivery_redacts_email_payload_and_keeps_idempotency_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory)
    route_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    sender = _FakeSender()
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_order_assigned_notification(session, persisted, route_id, now=now)
        await session.commit()

    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=sender,
        now=now,
    )

    assert summary.configured is True
    assert summary.attempted == 1 and summary.sent == 1 and summary.deferred == 0
    assert len(sender.messages) == 1
    assert sender.messages[0].recipient == "cliente@example.com"
    assert "Tu pedido fue asignado" in sender.messages[0].text_body
    assert point.order_id not in sender.messages[0].text_body
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.SENT
        assert event_row.recipient_email is None
        assert event_row.text_body == ""
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        assert (
            await enqueue_order_assigned_notification(session, persisted, route_id, now=now)
            == ()
        )

    repeated = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=sender,
        now=now + timedelta(minutes=5),
    )
    assert repeated.attempted == 0
    assert len(sender.messages) == 1


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_later_without_storing_exception_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory)
    route_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_courier_near_notification(session, persisted, route_id, now=now)
        await session.commit()

    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=_FakeSender(RuntimeError("private@example.com Carrera 25")),
        now=now,
    )

    assert summary.deferred == 1 and summary.sent == 0
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.PENDING
        assert event_row.available_at > now
        assert event_row.last_error_code == "RuntimeError"
        assert "private@example.com" not in event_row.last_error_code
        assert "Carrera 25" not in event_row.last_error_code
        retry_at = event_row.available_at

    retry = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=_FakeSender(),
        now=retry_at,
    )

    assert retry.attempted == 1 and retry.sent == 1 and retry.deferred == 0
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.SENT
        assert event_row.recipient_email is None
        assert event_row.text_body == ""


@pytest.mark.asyncio
async def test_notification_at_attempt_limit_is_terminal_and_redacts_pii(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_courier_near_notification(session, persisted, uuid4(), now=now)
        await session.flush()
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        event_row.attempt_count = MAX_NOTIFICATION_ATTEMPTS - 1
        await session.commit()

    failed_sender = _FakeSender(RuntimeError("private@example.com"))
    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=failed_sender,
        now=now,
    )

    assert summary.attempted == 1 and summary.deferred == 1
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.FAILED
        assert event_row.attempt_count == MAX_NOTIFICATION_ATTEMPTS
        assert event_row.recipient_email is None
        assert event_row.recipient_phone is None
        assert event_row.subject == ""
        assert event_row.text_body == ""
        assert event_row.lease_expires_at is None
        assert event_row.last_error_code == "RuntimeError"

    subsequent = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=_FakeSender(),
        now=now + timedelta(days=1),
    )
    assert subsequent.attempted == 0


@pytest.mark.asyncio
async def test_expired_final_attempt_is_redacted_without_an_extra_send(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        session.add(
            CustomerNotificationOutbox(
                id=uuid4(),
                delivery_point_id=point.id,
                event_key=f"expired-final-attempt-{point.id}",
                event_type=CustomerNotificationEvent.COURIER_NEAR,
                channel=CustomerNotificationChannel.EMAIL,
                recipient_email="private@example.com",
                subject="Private subject",
                text_body="Private body",
                status=CustomerNotificationStatus.PROCESSING,
                attempt_count=MAX_NOTIFICATION_ATTEMPTS,
                available_at=now - timedelta(hours=1),
                lease_expires_at=now - timedelta(seconds=1),
            )
        )
        await session.commit()

    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=_FakeSender(),
        now=now,
    )

    assert summary.attempted == 0
    async with session_factory() as session:
        row = await session.scalar(select(CustomerNotificationOutbox))
        assert row is not None
        assert row.status is CustomerNotificationStatus.FAILED
        assert row.attempt_count == MAX_NOTIFICATION_ATTEMPTS
        assert row.recipient_email is None
        assert row.subject == "" and row.text_body == ""
        assert row.last_error_code == "MaxAttemptsExceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [CustomerNotificationStatus.PENDING, CustomerNotificationStatus.PROCESSING],
)
async def test_revoking_email_consent_cancels_unsent_messages_and_redacts_contact(
    session_factory: async_sessionmaker[AsyncSession],
    status: CustomerNotificationStatus,
) -> None:
    point = await _create_point(session_factory)
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_order_assigned_notification(session, persisted, uuid4())
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        event_row.status = status
        cancelled = await cancel_pending_customer_notifications(session, point.id)
        await session.commit()

    assert cancelled == 1
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.CANCELLED
        assert event_row.recipient_email is None
        assert event_row.text_body == ""


@pytest.mark.asyncio
async def test_worker_rechecks_opt_in_after_claim_before_sending(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.customer_notifications as notifications

    point = await _create_point(session_factory)
    route_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_order_assigned_notification(session, persisted, route_id, now=now)
        await session.commit()

    claim = notifications._claim_pending_notifications

    async def claim_then_revoke(
        factory: async_sessionmaker[AsyncSession],
        claim_time: datetime,
        settings: Settings,
        enabled_channels: tuple[CustomerNotificationChannel, ...],
    ) -> list[tuple[UUID, CustomerNotificationChannel, str | None, str | None, str, str]]:
        claimed = await claim(factory, claim_time, settings, enabled_channels)
        async with session_factory() as session:
            async with session.begin():
                persisted = await session.get(DeliveryPoint, point.id)
                assert persisted is not None
                persisted.email_notifications_enabled = False
                await cancel_pending_customer_notifications(session, point.id)
        return claimed

    monkeypatch.setattr(notifications, "_claim_pending_notifications", claim_then_revoke)
    sender = _FakeSender()
    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=_smtp_settings(),
        sender=sender,
        now=now,
    )

    assert summary.attempted == summary.sent == summary.deferred == 0
    assert sender.messages == []
    async with session_factory() as session:
        event_row = await session.scalar(select(CustomerNotificationOutbox))
        assert event_row is not None
        assert event_row.status is CustomerNotificationStatus.CANCELLED
        assert event_row.recipient_email is None
        assert event_row.text_body == ""


class _FakeSmsSender:
    def __init__(self, failure: Exception | None = None) -> None:
        self.messages: list[CustomerSms] = []
        self.failure = failure

    def send(self, message: CustomerSms) -> None:
        if self.failure is not None:
            raise self.failure
        self.messages.append(message)


@pytest.mark.asyncio
async def test_sms_delivery_uses_twilio_channel_and_redacts_recipient_after_send(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    point = await _create_point(session_factory, opted_in=False, sms_opted_in=True)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with session_factory() as session:
        persisted = await session.get(DeliveryPoint, point.id)
        assert persisted is not None
        await enqueue_courier_near_notification(session, persisted, uuid4(), now=now)
        await session.commit()

    sms_sender = _FakeSmsSender()
    summary = await dispatch_pending_customer_notifications(
        session_factory,
        settings=Settings(
            notification_sms_twilio_account_sid="AC" + "b" * 32,
            notification_sms_twilio_auth_token=SecretStr("sms-test-token"),
            notification_sms_twilio_from_phone="+15005550006",
        ),
        sms_sender=sms_sender,
        now=now,
    )

    assert summary.configured is True
    assert summary.attempted == summary.sent == 1
    assert len(sms_sender.messages) == 1
    assert sms_sender.messages[0].recipient == "+573001234567"
    assert "500 m" in sms_sender.messages[0].text_body
    async with session_factory() as session:
        row = await session.scalar(select(CustomerNotificationOutbox))
        assert row is not None
        assert row.status is CustomerNotificationStatus.SENT
        assert row.recipient_phone is None
        assert row.text_body == ""
