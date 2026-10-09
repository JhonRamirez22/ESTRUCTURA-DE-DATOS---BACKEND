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


def csrf_matches(token: str, supplied: str | None, secret: str) -> bool:
    return (
        isinstance(supplied, str)
        and re.fullmatch(r"[0-9a-f]{64}", supplied) is not None
        and hmac.compare_digest(csrf_token(token, secret), supplied)
    )


async def create_access_session(
    session: AsyncSession,
    *,
    account_id: UUID | None = None,
    delivery_point_id: UUID | None = None,
    now: datetime | None = None,
) -> str:
    if (account_id is None) == (delivery_point_id is None):
        raise ValueError("Una sesión debe pertenecer a una cuenta o a un único pedido")
    instant = now or utc_now()
    raw_token = secrets.token_urlsafe(32)
    session.add(
        AccessSession(
            token_digest=token_digest(raw_token),
            account_id=account_id,
            delivery_point_id=delivery_point_id,
            created_at=instant,
            expires_at=instant + (
                STAFF_SESSION_LIFETIME if account_id else TRACKING_SESSION_LIFETIME
            ),
            last_seen_at=instant,
        )
    )
    await session.flush()
    return raw_token


async def resolve_access_session(
    session: AsyncSession,
    token: str | None,
    *,
    tracking: bool = False,
    now: datetime | None = None,
) -> SessionIdentity | None:
    if not token or not _TOKEN_PATTERN.fullmatch(token):
        return None
    result = await session.execute(
        select(AccessSession, Account)
        .outerjoin(Account, Account.id == AccessSession.account_id)
        .where(AccessSession.token_digest == token_digest(token))
    )
    row = result.one_or_none()
    if row is None:
        return None
    record, account = row
    instant = now or utc_now()
    if record.revoked_at is not None:
        return None
    expired = record.expires_at <= instant
    idle = record.account_id is not None and (
        record.last_seen_at + STAFF_SESSION_IDLE_LIMIT <= instant
    )
    disabled = record.account_id is not None and (account is None or not account.enabled)
    if expired or idle or disabled:
        record.revoked_at = instant
        await session.flush()
        return None
    if tracking != (record.delivery_point_id is not None):
        return None
    if instant - record.last_seen_at >= SESSION_TOUCH_INTERVAL:
        record.last_seen_at = instant
        await session.flush()
    return SessionIdentity(
        account_id=record.account_id,
        role=account.role if account else "customer",
        courier_id=account.courier_id if account else None,
        delivery_point_id=record.delivery_point_id,
        expires_at=record.expires_at,
    )


async def revoke_access_session(session: AsyncSession, token: str | None) -> None:
    if token and _TOKEN_PATTERN.fullmatch(token):
        await session.execute(
            update(AccessSession)
            .where(AccessSession.token_digest == token_digest(token))
            .values(revoked_at=utc_now())
        )


async def revoke_account_sessions(session: AsyncSession, account_id: UUID) -> None:
    await session.execute(
        update(AccessSession)
        .where(AccessSession.account_id == account_id, AccessSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
