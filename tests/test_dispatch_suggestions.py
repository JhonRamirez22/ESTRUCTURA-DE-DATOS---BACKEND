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
    old = _courier("c1", CourierStatus.AVAILABLE, age_min=30)
    assert ds.build_suggestions([old], [], NOW) == []
    assert ds.build_suggestions([old], [_pending("p1")], NOW)[0]["kind"] == "no_position"


def test_suggestions_are_capped() -> None:
    couriers = [_courier(f"c{i}", CourierStatus.ON_ROUTE, age_min=20) for i in range(9)]
    assert len(ds.build_suggestions(couriers, [], NOW)) == ds.MAX_SUGGESTIONS


def _groq(texts: dict[str, str]) -> httpx.MockTransport:
    content = json.dumps({"texts": texts})

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler)


@pytest.fixture
def ai_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_text_credentials", lambda: ("http://x", "k", "m", 1000))
    monkeypatch.setattr(ds, "_chat_json", li._chat_json)


def _card() -> list[dict[str, Any]]:
    return ds.build_suggestions(
        [_courier("c1", CourierStatus.ON_ROUTE, age_min=14, name="Beto")], [], NOW
    )


@pytest.mark.asyncio
async def test_verified_text_is_used_and_name_stays_local(ai_on: None) -> None:
    cards = _card()
    texts = {cards[0]["id"]: "{courier} lleva 14 min sin enviar ubicación."}
    out, status = await ds.phrase_with_ai(
        cards, budget=ds.DailyBudget(5), transport=_groq(texts)
    )
    assert out[0]["text"] == "Beto lleva 14 min sin enviar ubicación."
    assert out[0]["source"] == "groq-validated"
    assert "_name" not in out[0] and status["accepted"] == 1


@pytest.mark.asyncio
async def test_invented_figure_or_two_placeholders_fall_back(ai_on: None) -> None:
    cards = _card()
    fallback = cards[0]["text"]
    for bad in ("{courier} lleva 40 min sin ubicación.", "{courier} y {courier} llevan 14 min."):
        out, status = await ds.phrase_with_ai(
            cards, budget=ds.DailyBudget(5), transport=_groq({cards[0]["id"]: bad})
        )
        assert out[0]["text"] == fallback and status["accepted"] == 0


@pytest.mark.asyncio
async def test_missing_placeholder_gets_the_courier_name_prepended(ai_on: None) -> None:
    cards = _card()
    out, status = await ds.phrase_with_ai(
        cards, budget=ds.DailyBudget(5),
        transport=_groq({cards[0]["id"]: "GPS inactivo desde hace 14 minutos"}),
    )
    assert out[0]["text"] == "Beto: GPS inactivo desde hace 14 minutos"
    assert out[0]["source"] == "groq-validated" and status["accepted"] == 1

