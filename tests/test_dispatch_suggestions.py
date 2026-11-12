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


@pytest.mark.asyncio
async def test_daily_budget_stops_ai_calls_but_keeps_text(ai_on: None) -> None:
    cards = _card()
    budget = ds.DailyBudget(1)
    texts = {cards[0]["id"]: "{courier} lleva 14 min sin enviar ubicación."}
    await ds.phrase_with_ai(cards, budget=budget, transport=_groq(texts))
    out, status = await ds.phrase_with_ai(cards, budget=budget, transport=_groq(texts))
    assert out[0]["source"] == "deterministic"
    assert "tope diario" in status["reason"]


@pytest.mark.asyncio
async def test_without_credentials_everything_still_works() -> None:
    out, status = await ds.phrase_with_ai(_card(), budget=ds.DailyBudget(5))
    assert out[0]["source"] == "deterministic" and status["source"] == "deterministic"


def test_text_credentials_need_their_own_permission_not_the_location_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.settings import get_settings

    monkeypatch.setenv("CUSTOMER_CHAT_API_KEY", "clave-de-prueba")
    monkeypatch.setenv("CUSTOMER_CHAT_MODEL", "modelo")
    monkeypatch.setenv("AI_ALLOW_LOCATION_DATA_SHARING", "true")
    monkeypatch.setenv("AI_ALLOW_ROUTE_LOCATION_SHARING", "true")
    monkeypatch.setenv("AI_TEXT_PHRASING_ENABLED", "false")
    get_settings.cache_clear()
    assert li._text_credentials() is None  # el permiso de ubicaciones no basta

    monkeypatch.setenv("AI_TEXT_PHRASING_ENABLED", "true")
    monkeypatch.setenv("AI_ALLOW_LOCATION_DATA_SHARING", "false")
    monkeypatch.setenv("AI_ALLOW_ROUTE_LOCATION_SHARING", "false")
    get_settings.cache_clear()
    credentials = li._text_credentials()
    assert credentials is not None and credentials[1] == "clave-de-prueba"
    # Y el permiso de texto no abre las funciones que sí mandan ubicaciones.
    assert li._settings_credentials() is None

    monkeypatch.setenv("CUSTOMER_CHAT_API_KEY", "")
    get_settings.cache_clear()
    assert li._text_credentials() is None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_text_mode_leaves_room_for_reasoning_models(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"texts": {}}'}}]})

    model = "openai/gpt-oss-20b"
    monkeypatch.setattr(li, "_text_credentials", lambda: ("http://x", "k", model, 1000))
    await li._chat_json("sys", {"a": 1}, transport=httpx.MockTransport(handler), text_only=True)
    monkeypatch.setattr(li, "_settings_credentials", lambda: ("http://x", "k", model, 1000))
    await li._chat_json("sys", {"a": 1}, transport=httpx.MockTransport(handler))
    text_body, location_body = sent
    assert text_body["max_tokens"] == li.TEXT_MAX_TOKENS
    assert text_body["reasoning_effort"] == "low"
    # Las funciones que mandan ubicaciones conservan su petición de siempre.
    assert location_body["max_tokens"] == 500 and "reasoning_effort" not in location_body
