"""Lectura del buzón privado de notificaciones."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, notification: Notification) -> None:
        self._session.add(notification)

    async def list_for_recipient(self, recipient_id: str, limit: int) -> list[Notification]:
        notifications = await self._session.scalars(
            select(Notification)
            .where(Notification.recipient_id == recipient_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
        return list(notifications.all())
