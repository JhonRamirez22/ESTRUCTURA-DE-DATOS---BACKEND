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
    cards.append((5, _card(
        "shift_summary", "shift_summary", "info",
        f"Hoy: {len(todays)} {'ruta' if len(todays) == 1 else 'rutas'}, {delivered} "
        f"{'entrega' if delivered == 1 else 'entregas'} y {failed} fallidas; "
        f"la optimización ahorró {round(saved)} min.",
        {"routes": len(todays), "delivered": delivered, "failed": failed,
         "savedMinutes": round(saved)},
    )))

    total = delivered + failed
    if total >= MIN_DELIVERIES_FOR_RATE and failed / total >= FAILURE_RATE_ALERT:
        percent = round(100 * failed / total)
        cards.append((0, _card(
            "failure_rate", "failure_rate", "warning",
            f"{failed} de {total} entregas de hoy fallaron ({percent} %).",
            {"failed": failed, "total": total, "percent": percent},
        )))

    overruns: list[tuple[float, dict[str, Any]]] = []
    for route in todays:
        if route.estimated_minutes <= 0 or route.started_at is None:
            continue
        if route.status == RouteStatus.COMPLETED and route.completed_at is not None:
            actual = _minutes(route.started_at, route.completed_at)
            kind, verb = "route_overrun", "tardó"
        elif route.status == RouteStatus.IN_PROGRESS:
            actual = _minutes(route.started_at, now)
            kind, verb = "route_running_long", "lleva"
        else:
            continue
        extra = actual - route.estimated_minutes
        if actual >= OVERRUN_RATIO * route.estimated_minutes and extra >= OVERRUN_MIN_EXTRA_MINUTES:
            card = _card(
                f"{kind}:{route.ident}", kind, "warning",
                f"La ruta de {route.courier_name} {verb} {round(actual)} min "
                f"frente a {round(route.estimated_minutes)} estimados.",
                {
                    "actualMinutes": round(actual),
                    "estimatedMinutes": round(route.estimated_minutes),
                },
            )
            card["_name"] = route.courier_name
            overruns.append((extra, card))
    for _, card in sorted(overruns, key=lambda item: -item[0])[:2]:
        cards.append((1, card))

    cards.sort(key=lambda item: (item[0], item[1]["id"]))
    return [card for _, card in cards[:MAX_CARDS]]


def _card(
    card_id: str, kind: str, severity: str, text: str, figures: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": card_id,
        "kind": kind,
        "severity": severity,
        "text": text,
        "figures": figures,
        "source": "deterministic",
        "action": None,
    }
