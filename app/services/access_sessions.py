"""Tokens opacos: solo se conserva SHA-256, nunca la cookie reutilizable."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.security import AccessSession

STAFF_SESSION_LIFETIME = timedelta(hours=8)
STAFF_SESSION_IDLE_LIMIT = timedelta(minutes=30)
TRACKING_SESSION_LIFETIME = timedelta(minutes=15)
SESSION_TOUCH_INTERVAL = timedelta(seconds=60)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    account_id: UUID | None
    role: str
    courier_id: UUID | None
    delivery_point_id: UUID | None
    expires_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(token: str, secret: str) -> str:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("AUTH_SECRET debe contener al menos 32 bytes")
    return hmac.new(
        secret.encode("utf-8"), b"csrf:v1:" + token.encode(), hashlib.sha256
    ).hexdigest()
