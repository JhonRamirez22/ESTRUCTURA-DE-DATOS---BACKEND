"""Propiedades con entradas generadas: ninguna debe romper ni devolver algo inválido."""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.requests import Request

from app.core.location.pasto_area import contains_foreign_city_reference, normalize_pasto_address
from app.core.optimization.refine import refine_route, route_cost
from app.core.validation import plain_text
from app.services.client_identity import client_key

cost = st.one_of(st.none(), st.floats(min_value=0, max_value=500, allow_nan=False))


@st.composite
def matrices(draw: st.DrawFn) -> list[list[float | None]]:
    size = draw(st.integers(min_value=3, max_value=15))
    rows: list[list[float | None]] = []
    for source in range(size):
        rows.append([
            0.0 if source == target else draw(cost) for target in range(size)
        ])
    return rows


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(matrices())
def test_refinar_siempre_devuelve_una_permutacion_valida_y_no_peor(
    matrix: list[list[float | None]],
) -> None:
    start = list(range(len(matrix)))
    result = refine_route(matrix, start)
    assert result.route[0] == 0
    assert sorted(result.route) == start
    assert result.cost <= route_cost(matrix, start) or math.isinf(route_cost(matrix, start))
