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
    if not math.isinf(route_cost(matrix, start)):
        assert math.isfinite(result.cost)


@given(st.text(max_size=400))
def test_normalizar_direcciones_nunca_falla(text: str) -> None:
    normalized = normalize_pasto_address(text)
    assert isinstance(normalized, str)
    assert isinstance(contains_foreign_city_reference(normalized), bool)


@given(st.text(max_size=400))
def test_texto_plano_acepta_o_rechaza_con_valueerror(text: str) -> None:
    try:
        value = plain_text(text, max_length=100, field="campo")
    except ValueError:
        return
    assert len(value) <= 100
    assert "\x00" not in value
    assert "<" not in value or ">" not in value or "script" not in value.lower()


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=300))
def test_identidad_de_cliente_es_acotada_y_nunca_falla(forwarded: str) -> None:
    request = Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"x-forwarded-for", forwarded.encode("ascii"))],
        "client": ("127.0.0.1", 1234),
    })
    key = client_key(request)
    assert isinstance(key, str) and 0 < len(key) <= 64


@pytest.mark.parametrize("header", ["", "   ", ",,,", ", ,"])
def test_cabecera_vacia_cae_en_la_conexion_directa(header: str) -> None:
    request = Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"x-forwarded-for", header.encode())], "client": ("10.1.2.3", 1),
    })
    assert client_key(request) == "10.1.2.3"
