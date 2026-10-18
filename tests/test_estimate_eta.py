from __future__ import annotations

import pytest

from app.core.metrics.estimate_eta import estimate_stop_etas_from_matrix


def test_estimate_eta_accumulates_directed_minutes_and_preserves_disconnection() -> None:
    etas = estimate_stop_etas_from_matrix(
        ["first", "second", "third"],
        [[0, 5, 1, None], [None, 0, 8, 3], [None, None, 0, None], [0, 0, 0, 0]],
        [[0, 100, 900, None], [None, 0, 200, 300], [None, None, 0, None], [0, 0, 0, 0]],
    )
    assert etas[0].estimated_minutes_from_now == 5
    assert etas[0].distance_from_courier_meters == 100
    assert etas[1].estimated_minutes_from_now == 13
    assert etas[1].distance_from_courier_meters == 300
    assert etas[2].estimated_minutes_from_now is None
    assert etas[2].distance_from_courier_meters is None


def test_estimate_eta_handles_no_stops_and_rejects_invalid_matrix() -> None:
    assert estimate_stop_etas_from_matrix([], [[0]], [[0]]) == []
    with pytest.raises(ValueError, match="cuadrada"):
        estimate_stop_etas_from_matrix(["one"], [[0]], [[0]])
