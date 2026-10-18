"""Poller sencillo de la outbox para que los reintentos no dependan del siguiente request."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.customer_notifications import dispatch_pending_customer_notifications

_LOGGER = logging.getLogger(__name__)


async def run_customer_notification_worker(
    session_factory: async_sessionmaker[AsyncSession],
    stop_event: asyncio.Event,
    *,
    poll_interval_seconds: float = 30,
) -> None:
    """Process outbox batches periodically until FastAPI shuts down."""
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds debe ser positivo.")

    while not stop_event.is_set():
        try:
            summary = await dispatch_pending_customer_notifications(session_factory)
            if summary.deferred:
                _LOGGER.warning(
                    "El worker aplazó avisos del cliente",
                    extra={"deferred_count": summary.deferred},
                )
        except Exception as error:
            _LOGGER.warning(
                "El worker de avisos no pudo consultar la outbox",
                extra={"error_type": type(error).__name__},
            )

        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            continue
