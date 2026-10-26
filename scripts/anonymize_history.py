"""Anonymize shipment history older than a cutoff (manual command, never automatic).

Strips contact data and the street address from old shipments but keeps the figures
(status, timestamps, priority, route links) so reports stay accurate.

Usage:
    uv run python -m scripts.anonymize_history              # dry run, 12 months
    uv run python -m scripts.anonymize_history --apply      # really change rows
    uv run python -m scripts.anonymize_history --months 6 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.models import DeliveryPoint, DeliveryPointStatus
from app.services.access_sessions import utc_now

ANONYMIZED_ADDRESS = "Dirección anonimizada"
TERMINAL = (DeliveryPointStatus.DELIVERED, DeliveryPointStatus.FAILED)


class HistoryAnonymizer:
    """Removes personal data from finished shipments older than ``months``."""

    def __init__(self, session: AsyncSession, months: int = 12) -> None:
        if months < 1:
            raise ValueError("months must be at least 1")
        self._session = session
        self._cutoff: datetime = utc_now() - timedelta(days=30 * months)

    def _eligible(self) -> tuple[ColumnElement[bool], ...]:
        return (
            DeliveryPoint.status.in_(TERMINAL),
            DeliveryPoint.created_at < self._cutoff,
            DeliveryPoint.address != ANONYMIZED_ADDRESS,
        )

    async def count(self) -> int:
        total = await self._session.scalar(
            select(func.count()).select_from(DeliveryPoint).where(*self._eligible())
        )
        return int(total or 0)

    async def apply(self) -> int:
        result = await self._session.execute(
            update(DeliveryPoint).where(*self._eligible()).values(
                address=ANONYMIZED_ADDRESS, customer_email=None, customer_phone=None,
                email_notifications_enabled=False, sms_notifications_enabled=False,
            )
        )
        await self._session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def _main(months: int, apply: bool) -> None:
    async with get_session_factory()() as session:
        worker = HistoryAnonymizer(session, months)
        found = await worker.count()
        if not apply:
            print(f"Simulación: {found} envíos terminados con más de {months} meses.")
            print("Añade --apply para anonimizarlos.")
            return
        print(f"Anonimizados: {await worker.apply()} envíos.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main(args.months, args.apply))
