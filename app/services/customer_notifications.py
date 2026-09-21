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
