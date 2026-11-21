"""Ventanas horarias y prioridad: cumple lo posible, prioriza lo urgente, nunca empeora."""

from __future__ import annotations

import math
import random

import pytest

from app.core.optimization.time_windows import (
    Window,
    evaluate_route,
    plan_with_windows,
)


def _line(n: int, step: float = 5.0) -> list[list[float | None]]:
    """Nodos sobre una recta: ir de i a j cuesta step·|i-j| minutos."""
    return [[step * abs(i - j) for j in range(n)] for i in range(n)]


def test_evaluate_tracks_waiting_and_lateness() -> None:
    matrix = _line(3)
    windows = {1: Window(earliest=20.0), 2: Window(latest=8.0)}
    result = evaluate_route(matrix, [0, 1, 2], windows)
    # Llega al 1 en 5 pero espera hasta 20; al 2 llega en 25, con 17 min de retraso.
    assert result.arrivals == [0.0, 20.0, 25.0]
    assert result.late_stops == 1 and result.weighted_lateness == pytest.approx(17.0)
    assert not result.feasible


def test_unreachable_leg_is_never_free() -> None:
    matrix: list[list[float | None]] = [[0, None], [None, 0]]
    result = evaluate_route(matrix, [0, 1], {})
    assert math.isinf(result.finish_minutes) and not result.feasible


def test_a_deadline_turns_a_late_order_into_a_feasible_one() -> None:
    matrix = _line(4)
    windows = {3: Window(latest=16.0)}
    plan = plan_with_windows(matrix, [0, 2, 1, 3], windows)  # zigzag: llega al 3 en 25 min
    assert plan.before.late_stops == 1
    assert plan.after.feasible and plan.after.weighted_lateness == 0
    assert plan.route == [0, 1, 2, 3]


def test_priority_decides_who_is_late_when_not_everything_fits() -> None:
    # Dos paradas opuestas con el mismo plazo imposible de cumplir para ambas.
    matrix: list[list[float | None]] = [[0, 10, 10], [10, 0, 20], [10, 20, 0]]
    windows = {1: Window(latest=12.0), 2: Window(latest=12.0)}
    urgent_first = plan_with_windows(matrix, [0, 1, 2], windows, {1: 5.0, 2: 1.0})
    assert urgent_first.route[1] == 1
    urgent_second = plan_with_windows(matrix, [0, 1, 2], windows, {1: 1.0, 2: 5.0})
    assert urgent_second.route[1] == 2


def test_without_windows_the_order_is_left_alone_or_shortened() -> None:
    matrix = _line(5)
    plan = plan_with_windows(matrix, [0, 3, 1, 4, 2], {})
    assert plan.after.finish_minutes <= plan.before.finish_minutes
    assert plan.route[0] == 0


def test_rejects_duplicates_and_oversized_routes() -> None:
    with pytest.raises(ValueError):
        plan_with_windows(_line(3), [0, 1, 1], {})
    with pytest.raises(ValueError):
        plan_with_windows(_line(60), list(range(60)), {})


def test_tiny_routes_are_returned_unchanged() -> None:
    plan = plan_with_windows(_line(2), [0, 1], {1: Window(latest=1.0)})
    assert plan.route == [0, 1]


@pytest.mark.parametrize("seed", range(40))
def test_property_never_worse_origin_fixed_same_stops(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(3, 9)
    matrix: list[list[float | None]] = [
        [0.0 if i == j else float(rng.randint(1, 15)) for j in range(n)] for i in range(n)
    ]
    windows = {}
    for node in range(1, n):
        if rng.random() < 0.6:
            start = rng.uniform(0, 30)
            windows[node] = Window(start, start + rng.uniform(5, 40))
    priority = {node: float(rng.randint(1, 4)) for node in range(1, n)}
    route = [0, *rng.sample(range(1, n), n - 1)]
    plan = plan_with_windows(matrix, route, windows, priority, service_minutes=2.0)
    assert plan.route[0] == 0
    assert sorted(plan.route) == sorted(route)
    assert plan.after.objective <= plan.before.objective + 1e-6


def test_window_from_json_reads_local_clock_times() -> None:
    from app.core.optimization.time_windows import window_from_json

    now = 9 * 60 + 30.0  # 09:30
    window = window_from_json({"start": "10:00", "end": "11:15"}, now)
    assert window == Window(earliest=30.0, latest=105.0)
    assert window_from_json({"end": "09:45"}, now) == Window(earliest=0.0, latest=15.0)
    # Formas inválidas o contradictorias se ignoran: no inventan plazos.
    for bad in (None, "09:00", {}, {"start": "25:00"}, {"start": "11:00", "end": "10:00"},
                {"start": 900}, {"end": "9am"}):
        assert window_from_json(bad, now) is None


def test_planner_step_reorders_for_a_deadline_and_reports_lateness() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4

    from app.services.route_planner import _apply_time_windows

    ids = [uuid4(), uuid4(), uuid4()]
    # Hora local 08:00 (UTC 13:00). La parada 3 debe llegar antes de las 08:12.
    now = datetime(2026, 11, 20, 13, 0, tzinfo=UTC).replace(tzinfo=None)
    points = {
        ids[0]: SimpleNamespace(time_window=None, priority=1),
        ids[1]: SimpleNamespace(time_window=None, priority=1),
        ids[2]: SimpleNamespace(time_window={"end": "08:12"}, priority=3),
    }
    matrix = _line(4)
    summary = _apply_time_windows(matrix, [0, 1, 2, 3], ids, points, now)  # type: ignore[arg-type]
    assert summary is not None and summary["applied"] is False  # 15 min > 12: imposible
    assert summary["lateStopsBefore"] == 1

    points[ids[2]].time_window = {"end": "08:16"}
    fixed = _apply_time_windows(matrix, [0, 2, 1, 3], ids, points, now)  # type: ignore[arg-type]
    assert fixed is not None and fixed["applied"] is True
    assert fixed["route"] == [0, 1, 2, 3]
    assert fixed["lateStopsBefore"] == 1 and fixed["lateStopsAfter"] == 0
    assert fixed["lateMinutesAfter"] == 0

    # Sin ventanas ni prioridades el planificador no interviene.
    for point in points.values():
        point.time_window = None
    assert _apply_time_windows(matrix, [0, 1, 2, 3], ids, points, now) is None  # type: ignore[arg-type]
