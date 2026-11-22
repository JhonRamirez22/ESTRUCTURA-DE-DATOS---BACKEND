"""Live explanations: deterministic text first, Groq text only when every figure checks out."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.structures.ttl_cache import TtlCache
from app.services import live_explanations as le
from app.services import location_intelligence as li
from app.services.dispatch_suggestions import DailyBudget


def _move() -> le.Explanation:
    return le.Explanation(
        "move:s1", "move",
        "{stop} pasa de {from} a {to}: {to} llega antes y la flota ahorra 3,2 min.",
        {"gain": 3.2}, stop_id="s1", from_route_id="r1", to_route_id="r2",
    )


def _groq(texts: dict[str, str]) -> httpx.MockTransport:
    content = json.dumps({"texts": texts})
    return httpx.MockTransport(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )


@pytest.fixture(autouse=True)
def ai_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_text_credentials", lambda: ("http://x", "k", "m", 1000))
    monkeypatch.setattr(le, "_chat_json", li._chat_json)


def _phraser(texts: dict[str, str], budget: int = 5) -> le.ExplanationPhraser:
    return le.ExplanationPhraser(
        DailyBudget(budget), _groq(texts), TtlCache[str, dict[str, str]](8, 60.0)
    )


@pytest.mark.asyncio
async def test_verified_text_replaces_the_draft_and_keeps_placeholders() -> None:
    item = _move()
    text = "{stop} se mueve de {from} a {to} y se ahorran 3,2 min."
    status = await _phraser({"move:s1": text}).phrase([item])
    assert status == "ai" and item.source == "groq-validated"
    assert "{stop}" in item.text and "3,2" in item.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "{stop} pasa a {to} y ahorra 9 min.",  # invented figure
        "{stop} pasa de {from} a {to} por {to} y {to}.",  # one placeholder too many
    ],
)
async def test_invented_figures_or_extra_placeholders_fall_back(bad: str) -> None:
    item = _move()
    original = item.text
    await _phraser({"move:s1": bad}).phrase([item])
    assert item.text == original and item.source == "deterministic"


@pytest.mark.asyncio
async def test_same_proposal_is_phrased_once_then_served_from_cache() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        text = "{stop} pasa de {from} a {to}: ahorra 3,2 min."
        content = json.dumps({"texts": {"move:s1": text}})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    phraser = le.ExplanationPhraser(
        DailyBudget(5), httpx.MockTransport(handler), TtlCache[str, dict[str, str]](8, 60.0)
    )
    first, second = _move(), _move()
    assert await phraser.phrase([first]) == "ai"
    assert await phraser.phrase([second]) == "cached"
    assert len(calls) == 1 and second.source == "groq-validated"


@pytest.mark.asyncio
async def test_budget_exhausted_or_ai_down_keeps_deterministic_text() -> None:
    item = _move()
    assert await _phraser({}, budget=0).phrase([item]) == "deterministic"
    assert item.source == "deterministic"


@pytest.mark.asyncio
async def test_next_and_stable_explanations_are_never_sent_to_the_ai() -> None:
    items: list[Any] = [le.Explanation("next:r1", "next", "{route} va a {stop}", {"eta": 2.0})]
    assert await _phraser({"next:r1": "x"}).phrase(items) == "deterministic"
