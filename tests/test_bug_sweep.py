"""Defectos hallados en el barrido: cada prueba falla antes de su corrección."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.api import intelligence, order_requests
from app.services import location_intelligence as li


# --- H1: una cifra dentro de «2-opt» no es una cifra de ahorro -------------------------
def test_h1_algorithm_names_with_digits_are_not_treated_as_figures() -> None:
    text = "La mejora 2-opt bajó el tiempo y el ahorro total fue de 2,4 min frente a 34.6 min."
    assert li.verify_explanation(text, [2.4, 34.6])


def test_h1_invented_figures_are_still_rejected() -> None:
    assert not li.verify_explanation("La mejora 2-opt ahorró 9 min.", [2.4, 34.6])


# --- H6: sin minutos no se imprime «None» ----------------------------------------------
def test_h6_deterministic_explanation_never_prints_none() -> None:
    text = li.deterministic_explanation(
        {"savedMinutes": 3.0, "ladder": [{"step": "nearestNeighbor", "minutes": None}]}
    )
    assert "None" not in text


# --- H2: los resultados degradados no se quedan en caché -------------------------------
@pytest.mark.asyncio
async def test_h2_group_cache_does_not_keep_a_transient_ai_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intelligence._GROUP_CACHE.clear()
    calls = {"n": 0}

    async def flaky(pending: object) -> dict[str, object]:
        calls["n"] += 1
        ai = {"source": "deterministic", "proposed": calls["n"] > 1, "accepted": 0}
        if calls["n"] == 1:
            ai["reason"] = "La IA no respondió."
        return {"groups": [], "ai": ai}

    monkeypatch.setattr(intelligence, "suggest_groups", flaky)
    key = ("k",)
    first = await intelligence.cached_groups(key, [])
    second = await intelligence.cached_groups(key, [])
    assert first["ai"]["proposed"] is False
    assert second["ai"]["proposed"] is True  # volvió a consultar a la IA


@pytest.mark.asyncio
async def test_h2_explanation_cache_keeps_only_validated_text(
    monkeypatch: pytest.MonkeyPatch,
