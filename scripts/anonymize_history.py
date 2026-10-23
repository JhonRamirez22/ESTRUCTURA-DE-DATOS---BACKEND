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
