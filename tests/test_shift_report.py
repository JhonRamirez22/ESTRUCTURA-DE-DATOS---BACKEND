"""Resumen del turno y anomalías: cifras deterministas y texto de IA verificado."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from app.models.enums import RouteStatus
from app.services import dispatch_suggestions as ds
from app.services import location_intelligence as li
from app.services.shift_report import RouteView, build_shift_cards

# 14:00 hora de Bogotá del 20-nov-2026 (UTC-5).
NOW = datetime(2026, 11, 20, 19, 0, 0)


def _route(
    ident: str = "r1",
    *,
    name: str = "Ana",
    status: RouteStatus = RouteStatus.COMPLETED,
    estimated: float = 30.0,
    baseline: float | None = 40.0,
    started_min_ago: float | None = 120.0,
    run_minutes: float | None = 35.0,
    delivered: int = 3,
    failed: int = 0,
    created: datetime | None = None,
) -> RouteView:
    started = None if started_min_ago is None else NOW - timedelta(minutes=started_min_ago)
    completed = (
        started + timedelta(minutes=run_minutes)
        if started is not None and run_minutes is not None and status == RouteStatus.COMPLETED
        else None
    )
    return RouteView(
        ident, name, status, estimated, baseline, created or NOW - timedelta(hours=3),
        started, completed, delivered, failed,
    )


def _kinds(cards: list[dict[str, object]]) -> list[str]:
    return [str(card["kind"]) for card in cards]


def test_no_routes_today_means_no_cards() -> None:
    day_before = NOW - timedelta(days=1)
    old = RouteView(
        "r0", "Ana", RouteStatus.COMPLETED, 30, 40, day_before,
        day_before, day_before + timedelta(minutes=35), 3, 0,
    )
    assert build_shift_cards([old], NOW) == []
    assert build_shift_cards([], NOW) == []


def test_summary_counts_deliveries_and_savings() -> None:
    routes = [_route("a", delivered=3), _route("b", delivered=2, baseline=50)]
    cards = build_shift_cards(routes, NOW)
    summary = cards[-1]
    assert summary["kind"] == "shift_summary"
    assert summary["figures"] == {"routes": 2, "delivered": 5, "failed": 0, "savedMinutes": 30}


def test_failure_rate_alert_needs_enough_deliveries_and_threshold() -> None:
    few = build_shift_cards([_route(delivered=1, failed=1)], NOW)
    assert "failure_rate" not in _kinds(few)
