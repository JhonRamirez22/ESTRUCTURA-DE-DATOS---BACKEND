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
