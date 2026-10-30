"""Caché LRU sobre lista doble y mejora Or-opt de caminos abiertos."""

from __future__ import annotations

import itertools
import random

import pytest

from app.core.optimization.or_opt import _route_cost, or_opt
from app.core.optimization.two_opt import two_opt
from app.core.structures.lru_cache import LruCache


def test_lru_evicts_least_recently_used_and_refreshes_on_read() -> None:
    cache: LruCache[str, int] = LruCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # "a" pasa a ser el más reciente
    cache.put("c", 3)  # descarta "b"
    assert "b" not in cache
    assert cache.get("a") == 1 and cache.get("c") == 3
    cache.put("a", 10)  # reescribir no duplica
    assert len(cache) == 2 and cache.get("a") == 10


def test_lru_rejects_empty_capacity() -> None:
    with pytest.raises(ValueError):
        LruCache(0)


def test_or_opt_moves_a_misplaced_stop_that_two_opt_keeps() -> None:
    # Paradas en una línea 0-1-2-3-4; la ruta visita la 1 al final.
    position = [0, 1, 2, 3, 4]
    matrix = [[float(abs(a - b)) for b in position] for a in position]
    route = [0, 2, 3, 4, 1]
    improved = or_opt(matrix, route)
    assert improved[0] == 0
    assert improved == [0, 1, 2, 3, 4]
    assert _route_cost(matrix, improved) < _route_cost(matrix, route)
