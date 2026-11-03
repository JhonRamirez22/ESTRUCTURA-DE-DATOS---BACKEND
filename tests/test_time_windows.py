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


