"""Avisos al cliente por correo/SMS con consentimiento, outbox y reintentos."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Protocol
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CustomerNotificationChannel,
    CustomerNotificationEvent,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
    DeliveryPoint,
)
from app.settings import Settings, get_settings

_LOGGER = logging.getLogger(__name__)
_E164_PATTERN = re.compile(r"^\+\d{8,15}$")
EARTH_RADIUS_METERS = 6_371_000
# 500 m es un aviso de proximidad, no una promesa de ETA: las pendientes y
# calles estrechas de Pasto hacen engañosa una estimación de llegada más precisa.
NEARBY_DELIVERY_RADIUS_METERS = 500
OUTBOX_BATCH_LIMIT = 20
OUTBOX_LEASE_SECONDS = 90
RETRY_BASE_SECONDS = 30
RETRY_MAX_SECONDS = 3_600
MAX_NOTIFICATION_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class CustomerEmail:
    recipient: str
    subject: str
    text_body: str


@dataclass(frozen=True, slots=True)
class CustomerSms:
    recipient: str
    text_body: str


@dataclass(frozen=True, slots=True)
class DispatchSummary:
    configured: bool
    attempted: int = 0
    sent: int = 0
    deferred: int = 0
    warning: str | None = None


class EmailSender(Protocol):
    def send(self, message: CustomerEmail) -> None: ...


class SmsSender(Protocol):
    def send(self, message: CustomerSms) -> None: ...


class SmtpEmailSender:
    """Entrega correo por SMTP con STARTTLS (o TLS implícito en el puerto 465)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send(self, message: CustomerEmail) -> None:
        settings = self._settings
        host = (settings.notification_smtp_host or "").strip()
        email_message = EmailMessage()
        email_message["From"] = settings.notification_from_email or ""
        email_message["To"] = message.recipient
        email_message["Subject"] = message.subject
        email_message.set_content(message.text_body)
        timeout = settings.notification_smtp_timeout_ms / 1_000
        context = ssl.create_default_context()

        if settings.notification_smtp_port == 465:
            with smtplib.SMTP_SSL(
                host,
                settings.notification_smtp_port,
                timeout=timeout,
                context=context,
            ) as client:
                self._authenticate(client)
                client.send_message(email_message)
            return

        with smtplib.SMTP(
            host,
            settings.notification_smtp_port,
            timeout=timeout,
        ) as client:
            client.starttls(context=context)
            self._authenticate(client)
            client.send_message(email_message)

    def _authenticate(self, client: smtplib.SMTP) -> None:
        username = (self._settings.notification_smtp_username or "").strip()
        password = self._settings.notification_smtp_password
        if username and password is not None:
            client.login(username, password.get_secret_value())


class TwilioSmsSender:
    """Envía SMS mediante HTTPS; el número solo llega al proveedor con opt-in."""

    _API_ROOT = "https://api.twilio.com/2010-04-01/Accounts"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def send(self, message: CustomerSms) -> None:
        settings = self._settings
        account_sid = (settings.notification_sms_twilio_account_sid or "").strip()
        auth_token = settings.notification_sms_twilio_auth_token
        from_phone = (settings.notification_sms_twilio_from_phone or "").strip()
        if not customer_sms_delivery_configured(settings):
            raise ValueError("La configuración SMS de Twilio está incompleta.")
        if not _E164_PATTERN.fullmatch(message.recipient):
            raise ValueError("El teléfono destinatario no está en formato E.164.")

        with httpx.Client(
            timeout=settings.notification_sms_timeout_ms / 1_000,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            response = client.post(
                f"{self._API_ROOT}/{account_sid}/Messages.json",
                auth=(account_sid, auth_token.get_secret_value() if auth_token else ""),
                data={"From": from_phone, "To": message.recipient, "Body": message.text_body},
            )
        response.raise_for_status()


def customer_email_delivery_configured(settings: Settings | None = None) -> bool:
    active_settings = settings or get_settings()
    host = (active_settings.notification_smtp_host or "").strip()
    sender = (active_settings.notification_from_email or "").strip()
    username = (active_settings.notification_smtp_username or "").strip()
    password = active_settings.notification_smtp_password
    has_password = password is not None and bool(password.get_secret_value())
    return bool(
        host
        and sender
        and "\r" not in sender
        and "\n" not in sender
        and 1 <= active_settings.notification_smtp_port <= 65_535
        and active_settings.notification_smtp_timeout_ms > 0
        and bool(username) == has_password
    )


def customer_sms_delivery_configured(settings: Settings | None = None) -> bool:
    active_settings = settings or get_settings()
    account_sid = (active_settings.notification_sms_twilio_account_sid or "").strip()
    auth_token = active_settings.notification_sms_twilio_auth_token
    from_phone = (active_settings.notification_sms_twilio_from_phone or "").strip()
    return bool(
        re.fullmatch(r"AC[a-fA-F0-9]{32}", account_sid) is not None
        and auth_token is not None
        and auth_token.get_secret_value().strip()
        and _E164_PATTERN.fullmatch(from_phone)
        and active_settings.notification_sms_timeout_ms > 0
    )


def customer_notification_channels_configured(
    settings: Settings | None = None,
) -> dict[CustomerNotificationChannel, bool]:
    active_settings = settings or get_settings()
    return {
        CustomerNotificationChannel.EMAIL: customer_email_delivery_configured(active_settings),
        CustomerNotificationChannel.SMS: customer_sms_delivery_configured(active_settings),
    }


def any_customer_notification_delivery_configured(settings: Settings | None = None) -> bool:
    return any(customer_notification_channels_configured(settings).values())


async def enqueue_order_assigned_notification(
    session: AsyncSession,
    point: DeliveryPoint,
    route_id: UUID,
    *,
    now: datetime | None = None,
) -> tuple[CustomerNotificationChannel, ...]:
    return await _enqueue_customer_event(
        session,
        point,
        route_id,
        CustomerNotificationEvent.ORDER_ASSIGNED,
        subject="Tu pedido ya fue asignado",
        text_body=(
            "Tu pedido fue asignado a una ruta de entrega. "
            "Te avisaremos cuando el repartidor esté cerca."
        ),
        sms_body=(
            "Rutas Pasto: tu pedido ya fue asignado. "
            "Te avisaremos cuando el repartidor esté cerca."
        ),
        now=now,
    )


async def enqueue_courier_near_notification(
    session: AsyncSession,
    point: DeliveryPoint,
    route_id: UUID,
    *,
    now: datetime | None = None,
) -> tuple[CustomerNotificationChannel, ...]:
    return await _enqueue_customer_event(
        session,
        point,
        route_id,
        CustomerNotificationEvent.COURIER_NEAR,
        subject="Tu repartidor está cerca",
        text_body=(
            "El repartidor asignado a tu pedido está a menos de "
            f"{NEARBY_DELIVERY_RADIUS_METERS} m del destino. La distancia es aproximada "
            "y no representa una hora exacta de llegada."
        ),
        sms_body=(
            "Rutas Pasto: el repartidor asignado a tu pedido está a menos de "
            f"{NEARBY_DELIVERY_RADIUS_METERS} m. Distancia aproximada, no es una hora exacta."
        ),
        now=now,
    )


async def _enqueue_customer_event(
    session: AsyncSession,
    point: DeliveryPoint,
    route_id: UUID,
    event_type: CustomerNotificationEvent,
    *,
    subject: str,
    text_body: str,
    sms_body: str,
    now: datetime | None,
) -> tuple[CustomerNotificationChannel, ...]:
    queued: list[CustomerNotificationChannel] = []
    email = (point.customer_email or "").strip()
    if point.email_notifications_enabled and email:
        inserted = await _enqueue_notification(
            session,
            point,
            route_id,
            event_type,
            CustomerNotificationChannel.EMAIL,
            email,
            subject,
            text_body,
            now,
        )
        if inserted:
            queued.append(CustomerNotificationChannel.EMAIL)

    phone = (point.customer_phone or "").strip()
    if (
        point.sms_notifications_enabled
        and phone
        and _E164_PATTERN.fullmatch(phone)
    ):
        inserted = await _enqueue_notification(
            session,
            point,
            route_id,
            event_type,
            CustomerNotificationChannel.SMS,
            phone,
            subject,
            sms_body,
            now,
        )
        if inserted:
            queued.append(CustomerNotificationChannel.SMS)
    return tuple(queued)


async def cancel_pending_customer_notifications(
    session: AsyncSession,
    delivery_point_id: UUID,
) -> int:
    """Cancela avisos aún no enviados y elimina destinatarios y contenido."""
    result = await session.execute(
        update(CustomerNotificationOutbox)
        .where(
            CustomerNotificationOutbox.delivery_point_id == delivery_point_id,
            CustomerNotificationOutbox.status.in_(
                (
                    CustomerNotificationStatus.PENDING,
                    CustomerNotificationStatus.PROCESSING,
                )
            ),
        )
        .values(
            status=CustomerNotificationStatus.CANCELLED,
            recipient_email=None,
            recipient_phone=None,
            subject="",
            text_body="",
            lease_expires_at=None,
        )
        .returning(CustomerNotificationOutbox.id)
    )
    return len(result.scalars().all())


async def dispatch_pending_customer_notifications(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings | None = None,
    sender: EmailSender | None = None,
    sms_sender: SmsSender | None = None,
    now: datetime | None = None,
) -> DispatchSummary:
    """Entrega canales configurados fuera de la transacción y conserva fallos para reintento."""
    active_settings = settings or get_settings()
    configured_channels = customer_notification_channels_configured(active_settings)
    enabled_channels = tuple(channel for channel, enabled in configured_channels.items() if enabled)
    if not enabled_channels:
        return DispatchSummary(
            configured=False,
            warning="Avisos en cola: configura SMTP y/o las credenciales SMS de Twilio.",
        )

    active_email_sender = sender or SmtpEmailSender(active_settings)
    active_sms_sender = sms_sender or TwilioSmsSender(active_settings)
    current_time = _as_naive_utc(now or datetime.now(UTC))
    claimed = await _claim_pending_notifications(
        session_factory,
        current_time,
        active_settings,
        enabled_channels,
    )
    sent = 0
    attempted = 0
    deferred = 0

    for notification_id, channel, email, phone, subject, text_body in claimed:
        if not await _has_current_delivery_consent(session_factory, notification_id):
            continue
        attempted += 1
        try:
            if channel is CustomerNotificationChannel.EMAIL:
                await asyncio.to_thread(
                    active_email_sender.send,
                    CustomerEmail(email or "", subject, text_body),
                )
            else:
                await asyncio.to_thread(
                    active_sms_sender.send,
                    CustomerSms(phone or "", text_body),
                )
        except Exception as error:
            await _record_delivery_failure(
                session_factory,
                notification_id,
                current_time,
                type(error).__name__,
            )
            deferred += 1
        else:
            if await _record_delivery_success(session_factory, notification_id, current_time):
                sent += 1

    return DispatchSummary(
        configured=True,
        attempted=attempted,
        sent=sent,
        deferred=deferred,
    )


async def dispatch_pending_customer_notifications_background(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Punto de entrada para BackgroundTasks; no bloquea la respuesta HTTP."""
    summary = await dispatch_pending_customer_notifications(session_factory)
    if summary.deferred:
        _LOGGER.warning(
            "Se aplazó la entrega de avisos al cliente",
            extra={"deferred_count": summary.deferred},
        )


def is_courier_near_delivery_point(
    courier_latitude: float,
    courier_longitude: float,
    point_latitude: float,
    point_longitude: float,
) -> bool:
    """Determina proximidad geodésica aproximada; no calcula calles ni tiempo de llegada."""
    if not all(
        math.isfinite(value)
        for value in (courier_latitude, courier_longitude, point_latitude, point_longitude)
    ):
        return False
    latitude_delta = math.radians(point_latitude - courier_latitude)
    longitude_delta = math.radians(point_longitude - courier_longitude)
    start_latitude = math.radians(courier_latitude)
    end_latitude = math.radians(point_latitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(start_latitude) * math.cos(end_latitude) * math.sin(longitude_delta / 2) ** 2
    )
    distance_meters = 2 * EARTH_RADIUS_METERS * math.asin(min(1.0, math.sqrt(haversine)))
    return distance_meters <= NEARBY_DELIVERY_RADIUS_METERS


async def _enqueue_notification(
    session: AsyncSession,
    point: DeliveryPoint,
    route_id: UUID,
    event_type: CustomerNotificationEvent,
    channel: CustomerNotificationChannel,
    recipient: str,
    subject: str,
    text_body: str,
    now: datetime | None,
) -> bool:
    # Mantener la clave de email histórica preserva idempotencia al migrar desde 0002.
    event_key = f"{event_type.value.lower()}:{route_id}:{point.id}"
    if channel is CustomerNotificationChannel.SMS:
        event_key = f"{event_key}:sms"
    values = {
        "id": uuid4(),
        "delivery_point_id": point.id,
        "event_key": event_key,
        "event_type": event_type,
        "channel": channel,
        "recipient_email": recipient if channel is CustomerNotificationChannel.EMAIL else None,
        "recipient_phone": recipient if channel is CustomerNotificationChannel.SMS else None,
        "subject": subject,
        "text_body": text_body,
