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
