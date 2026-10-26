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
) -> None:
    intelligence._EXPLAIN_CACHE.clear()
    answers = iter(
        [
            {"text": "determinista", "source": "deterministic"},
            {"text": "de groq", "source": "groq-validated"},
        ]
    )

    async def fake_explain(summary: object) -> dict[str, str]:
        return next(answers)

    monkeypatch.setattr(intelligence, "explain_route", fake_explain)
    first = await intelligence.cached_explanation("r1:t", {})
    second = await intelligence.cached_explanation("r1:t", {})
    assert first["source"] == "deterministic"
    assert second["source"] == "groq-validated"
    third = await intelligence.cached_explanation("r1:t", {})
    assert third["cached"] is True  # el texto validado sí se conserva


# --- H3: un error de base de datos al buscar paradas cercanas no escapa ----------------
class _BrokenRepository:
    async def active_points_near(self, *args: object) -> list[object]:
        raise SQLAlchemyError("boom")


@pytest.mark.asyncio
async def test_h3_nearby_lookup_failure_does_not_block_the_request() -> None:
    notice = await order_requests._nearby_stop_notice(_BrokenRepository(), 1.21, -77.28)  # type: ignore[arg-type]
    assert notice is None


# --- H5: la tarea de vaciado de la cola queda referenciada -----------------------------
@pytest.mark.asyncio
async def test_h5_queue_keeps_a_reference_to_its_drain_task() -> None:
    from app.services.location_event_queue import LocationEventQueue

    queue = LocationEventQueue(max_pending=4)

    async def work() -> int:
        return 1

    assert await queue.enqueue(work) == 1
    assert hasattr(queue, "_drain_task")



# --- H4: guardar el historial nunca debe romper el despacho ----------------------------
@pytest.mark.asyncio
async def test_h4_traffic_history_failure_does_not_break_the_dispatch_transaction(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.base import Base
    from app.models import TrafficObservation
    from app.services import route_planner
    from app.settings import get_settings

    monkeypatch.setenv("TRAFFIC_HISTORY_ENABLED", "true")
    get_settings.cache_clear()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/h4.sqlite")
    async with engine.begin() as connection:
        tables = [t for t in Base.metadata.sorted_tables if t.name != "traffic_observations"]
        await connection.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    observation = {
        "fromStopId": "stop-0", "toStopId": "stop-1", "trafficMultiplier": 1.8,
        "level": "ALTO", "roadClosure": False, "point": [-77.28, 1.21],
        "observedAt": "2026-10-26T15:00:00+00:00",
    }
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            # La tabla no existe: antes de la corrección esto lanzaba y rompía la transacción.
            await route_planner._persist_traffic_history(
                session, uuid4(), {"observations": [observation]}
            )
            await session.commit()
            await session.execute(
                __import__("sqlalchemy").text("select 1")
            )  # la sesión sigue utilizable
    finally:
        await engine.dispose()
        get_settings.cache_clear()
    assert TrafficObservation.__tablename__ == "traffic_observations"


# --- H2b: una búsqueda de dirección sin candidatos no se queda en caché -----------------
@pytest.mark.asyncio
async def test_h2_empty_address_lookup_is_not_cached() -> None:
    order_requests._LOOKUP_CACHE.clear()
    assert order_requests.should_cache_lookup({"candidates": [], "warning": "x"}) is False
    assert order_requests.should_cache_lookup({"candidates": [{"lat": 1}], "warning": None}) is True


# --- H7: el agrupamiento compara todos los pares: acotar la entrada ---------------------
def test_h7_group_input_is_capped_so_the_event_loop_is_not_blocked() -> None:
    pending = [
        li.PendingLocation(
            ident=f"id-{i}", alias=f"p-{i}", lat=1.2136 + (i % 40) * 0.0002,
            lng=-77.2811 + (i // 40) * 0.0002,
        )
        for i in range(1000)
    ]
    groups = li.build_groups(pending)
    used = {member for group in groups for member in group["members"]}
    assert len(used) <= li.MAX_GROUP_INPUT
