"""Resumen del turno y alertas de anomalía: el cálculo las produce, la IA solo las redacta.

Todo sale de rutas y pedidos del día (hora de Bogotá). Las cifras de cada tarjeta son las que
verifica ``dispatch_suggestions.phrase_with_ai``; sin IA se muestra el texto determinista.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.models.enums import RouteStatus

_BOGOTA_OFFSET = timedelta(hours=-5)
MIN_DELIVERIES_FOR_RATE = 4
FAILURE_RATE_ALERT = 0.25
OVERRUN_RATIO = 1.5
OVERRUN_MIN_EXTRA_MINUTES = 10.0
MAX_CARDS = 6


@dataclass(frozen=True, slots=True)
class RouteView:
    ident: str
    courier_name: str
    status: RouteStatus
    estimated_minutes: float
    baseline_minutes: float | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    delivered: int
    failed: int


def _local_day(moment: datetime) -> object:
    return (moment + _BOGOTA_OFFSET).date()


def _minutes(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 60.0)


def build_shift_cards(routes: Sequence[RouteView], now: datetime) -> list[dict[str, Any]]:
    today = _local_day(now)
    todays = [
        route for route in routes
        if _local_day(route.created_at) == today
        or (route.completed_at is not None and _local_day(route.completed_at) == today)
    ]
    if not todays:
        return []
    cards: list[tuple[int, dict[str, Any]]] = []

    delivered = sum(route.delivered for route in todays)
    failed = sum(route.failed for route in todays)
    saved = sum(
        max(0.0, route.baseline_minutes - route.estimated_minutes)
        for route in todays
        if route.baseline_minutes is not None and route.status != RouteStatus.CANCELLED
    )
