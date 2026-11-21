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
    low = build_shift_cards([_route(delivered=9, failed=1)], NOW)
    assert "failure_rate" not in _kinds(low)
    high = build_shift_cards([_route(delivered=3, failed=2)], NOW)
    alert = next(card for card in high if card["kind"] == "failure_rate")
    assert alert["figures"] == {"failed": 2, "total": 5, "percent": 40}
    assert high[0]["kind"] == "failure_rate"  # las alertas van antes que el resumen


def test_overrun_needs_both_ratio_and_extra_minutes() -> None:
    slight = build_shift_cards([_route(estimated=30, run_minutes=40)], NOW)
    assert "route_overrun" not in _kinds(slight)
    short = build_shift_cards([_route(estimated=5, run_minutes=9)], NOW)  # 1,8x pero +4 min
    assert "route_overrun" not in _kinds(short)
    late = build_shift_cards([_route(estimated=30, run_minutes=55)], NOW)
    overrun = next(card for card in late if card["kind"] == "route_overrun")
    assert overrun["figures"] == {"actualMinutes": 55, "estimatedMinutes": 30}
    assert "Ana" in str(overrun["text"])


def test_route_still_running_too_long_is_flagged() -> None:
    running = _route(status=RouteStatus.IN_PROGRESS, estimated=30, started_min_ago=70)
    cards = build_shift_cards([running], NOW)
    assert "route_running_long" in _kinds(cards)


def test_cancelled_and_unstarted_routes_never_raise_overrun() -> None:
    cancelled = _route(status=RouteStatus.CANCELLED, run_minutes=None)
    planned = _route("p", status=RouteStatus.PLANNED, started_min_ago=None, run_minutes=None)
    cards = build_shift_cards([cancelled, planned], NOW)
    assert _kinds(cards) == ["shift_summary"]


def _groq(texts: dict[str, str]) -> httpx.MockTransport:
    content = json.dumps({"texts": texts})
    return httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )


@pytest.mark.asyncio
async def test_ai_text_without_courier_must_not_contain_placeholder_and_keep_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(li, "_settings_credentials", lambda: ("http://x", "k", "m", 1000))
    cards = build_shift_cards([_route(delivered=3, failed=2)], NOW)
    alert = next(card for card in cards if card["kind"] == "failure_rate")
    good = "Fallaron 2 de 5 entregas, un 40 %."
    out, status = await ds.phrase_with_ai(
        [alert], budget=ds.DailyBudget(5), transport=_groq({str(alert["id"]): good})
    )
    assert out[0]["text"] == good and status["accepted"] == 1
    for bad in ("Fallaron {courier} 2 de 5.", "Fallaron 3 de 5 entregas."):
        fallback, state = await ds.phrase_with_ai(
            [alert], budget=ds.DailyBudget(5), transport=_groq({str(alert["id"]): bad})
        )
        assert fallback[0]["text"] == alert["text"] and state["accepted"] == 0


@pytest.mark.asyncio
async def test_overrun_card_keeps_name_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_settings_credentials", lambda: ("http://x", "k", "m", 1000))
    cards = build_shift_cards([_route(estimated=30, run_minutes=55)], NOW)
    overrun = next(card for card in cards if card["kind"] == "route_overrun")
    text = "La ruta de {courier} tardó 55 min frente a 30 estimados."
    out, _ = await ds.phrase_with_ai(
        [overrun], budget=ds.DailyBudget(5), transport=_groq({str(overrun["id"]): text})
    )
    assert out[0]["text"].startswith("La ruta de Ana") and "_name" not in out[0]
