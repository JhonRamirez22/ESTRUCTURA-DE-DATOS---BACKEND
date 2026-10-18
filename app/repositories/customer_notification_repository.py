"""Agregados del outbox de avisos externos sin devolver datos de contacto."""

from __future__ import annotations

from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CustomerNotificationChannel,
    CustomerNotificationOutbox,
    CustomerNotificationStatus,
)


class ChannelOutboxCounts(TypedDict):
    pending: int
    retrying: int
    processing: int
    failed: int


class CustomerNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_outbox_counts(
        self,
    ) -> dict[CustomerNotificationChannel, ChannelOutboxCounts]:
        counts: dict[CustomerNotificationChannel, ChannelOutboxCounts] = {
            channel: {"pending": 0, "retrying": 0, "processing": 0, "failed": 0}
            for channel in CustomerNotificationChannel
        }
        rows = await self._session.execute(
            select(
                CustomerNotificationOutbox.channel,
                CustomerNotificationOutbox.status,
                func.count(CustomerNotificationOutbox.id),
            )
            .where(
                CustomerNotificationOutbox.status.in_(
                    (
                        CustomerNotificationStatus.PENDING,
                        CustomerNotificationStatus.PROCESSING,
                        CustomerNotificationStatus.FAILED,
                    )
                )
            )
            .group_by(
                CustomerNotificationOutbox.channel,
                CustomerNotificationOutbox.status,
            )
        )
        for channel, status, count in rows:
            if status is CustomerNotificationStatus.PENDING:
                counts[channel]["pending"] = count
            elif status is CustomerNotificationStatus.PROCESSING:
                counts[channel]["processing"] = count
            elif status is CustomerNotificationStatus.FAILED:
                counts[channel]["failed"] = count

        retry_rows = await self._session.execute(
            select(
                CustomerNotificationOutbox.channel,
                func.count(CustomerNotificationOutbox.id),
            )
            .where(
                CustomerNotificationOutbox.status == CustomerNotificationStatus.PENDING,
                CustomerNotificationOutbox.last_error_code.is_not(None),
            )
            .group_by(CustomerNotificationOutbox.channel)
        )
        for channel, count in retry_rows:
            counts[channel]["retrying"] = count
        return counts
