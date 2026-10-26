"""IA de ubicaciones: Groq propone y el chequeo valida; las cifras de la explicación cuadran."""

from __future__ import annotations

import httpx
import pytest

from app.services import location_intelligence as li


def _pending(*coords: tuple[float, float]) -> list[li.PendingLocation]:
    return [
        li.PendingLocation(ident=f"id-{i}", alias=f"p-{i}", lat=lat, lng=lng)
        for i, (lat, lng) in enumerate(coords)
    ]


def test_candidate_groups_are_deterministic_and_within_radius() -> None:
    groups = li.build_groups(_pending((1.2136, -77.2811), (1.2140, -77.2811), (1.2500, -77.2500)))
    assert [group["members"] for group in groups] == [["p-0", "p-1"]]
    assert groups[0]["source"] == "deterministic"


def test_validation_rejects_invented_repeated_and_oversized_members() -> None:
    candidates = [
        {
            "members": ["p-0", "p-1", "p-2"],
            "spreadMeters": 120,
            "source": "deterministic",
            "reason": None,
        }
    ]
    accepted = li._validate_group_proposal(
        {
            "groups": [
                {"members": ["p-0", "p-9"], "reason": "inventado"},
                {"members": ["p-0", "p-0"], "reason": "repetido"},
                {"members": ["p-1", "p-2"], "reason": "válido"},
                {"members": ["p-2", "p-1"], "reason": "ya usado"},
            ]
        },
        candidates,
    )
    assert [group["members"] for group in accepted] == [["p-1", "p-2"]]
    assert accepted[0]["source"] == "groq-validated"


def test_explanation_numbers_must_match_verified_summary() -> None:
    assert li.verify_explanation("Ahorra 2,4 min frente a 34.6 min.", [2.4, 34.6])
    assert not li.verify_explanation("Ahorra 9 min.", [2.4, 34.6])


def test_deterministic_explanation_without_savings() -> None:
    text = li.deterministic_explanation({"savedMinutes": 0.0, "ladder": []})
    assert "ya era el mejor" in text


@pytest.mark.asyncio
async def test_groq_unavailable_falls_back_to_deterministic_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(li, "_settings_credentials", lambda: None)
    result = await li.suggest_groups(_pending((1.2136, -77.2811), (1.2140, -77.2811)))
    assert result["ai"]["proposed"] is False
    assert result["groups"][0]["source"] == "deterministic"


@pytest.mark.asyncio
async def test_groq_proposal_is_used_only_after_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(li, "_settings_credentials", lambda: ("https://groq.test", "k", "m", 1000))

    def handler(request: httpx.Request) -> httpx.Response:
        content = '{"groups":[{"members":["p-0","p-1"],"reason":"misma cuadra"}]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = await li.suggest_groups(
        _pending((1.2136, -77.2811), (1.2140, -77.2811)),
        transport=httpx.MockTransport(handler),
    )
    assert result["ai"]["source"] == "groq-validated"
    assert result["groups"][0]["reason"] == "misma cuadra"
