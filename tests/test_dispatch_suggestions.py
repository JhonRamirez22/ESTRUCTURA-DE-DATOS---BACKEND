"""Sugerencias en contexto: reglas deterministas, texto de IA verificado, tope diario."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from app.models.enums import CourierStatus
from app.services import dispatch_suggestions as ds
from app.services import location_intelligence as li

NOW = datetime(2026, 11, 20, 12, 0, 0)
BASE = (1.2136, -77.2811)


def _courier(
    ident: str, status: CourierStatus, *, age_min: float | None, name: str = "Ana"
) -> ds.CourierView:
    moved = None if age_min is None else NOW - timedelta(minutes=age_min)
    return ds.CourierView(ident, name, status, BASE[0], BASE[1], moved)


def _pending(ident: str, dlat: float = 0.002) -> ds.PendingView:
    return ds.PendingView(ident, BASE[0] + dlat, BASE[1])


def test_idle_courier_gets_nearest_orders_with_select_action() -> None:
    cards = ds.build_suggestions(
        [_courier("c1", CourierStatus.AVAILABLE, age_min=0.5)],
        [_pending(f"p{i}", 0.001 * i) for i in range(1, 5)],
        NOW,
    )
    assert [card["kind"] for card in cards] == ["idle_courier"]
    action = cards[0]["action"]
    assert action["deliveryPointIds"] == ["p1", "p2", "p3"]
    assert cards[0]["figures"]["orders"] == 3


def test_far_orders_and_no_pending_give_no_idle_suggestion() -> None:
    far = [_pending("p1", 0.5)]
    free = _courier("c1", CourierStatus.AVAILABLE, age_min=0)
    assert ds.build_suggestions([free], far, NOW) == []
    assert ds.build_suggestions([free], [], NOW) == []


def test_stale_gps_on_route_is_urgent_and_comes_first() -> None:
    cards = ds.build_suggestions(
        [
            _courier("c1", CourierStatus.AVAILABLE, age_min=0, name="Ana"),
            _courier("c2", CourierStatus.ON_ROUTE, age_min=14, name="Beto"),
        ],
        [_pending("p1")],
        NOW,
    )
    assert cards[0]["kind"] == "stale_gps"
    assert cards[0]["figures"] == {"minutes": 14}
    assert "Beto" in cards[0]["text"]


def test_available_without_fresh_position_is_flagged_only_with_pending_orders() -> None:
