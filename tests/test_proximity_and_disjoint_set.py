"""Unión-find y agrupamiento por radio: cercanía real en metros y tope de capacidad."""

from __future__ import annotations

import pytest

from app.core.location.proximity import Located, cluster_by_radius, haversine_m, nearest_within
from app.core.structures.disjoint_set import DisjointSet


def test_disjoint_set_unions_and_reports_groups() -> None:
    ds = DisjointSet(["a", "b", "c", "d"])
    assert ds.union("a", "b") is True
    assert ds.union("b", "a") is False
    ds.union("c", "d")
    assert ds.size_of("a") == 2
    assert sorted(sorted(group) for group in ds.groups()) == [["a", "b"], ["c", "d"]]


def test_haversine_matches_known_pasto_distance() -> None:
    # Dos puntos separados ~0.001° de latitud ≈ 111 m.
    assert haversine_m(1.2136, -77.2811, 1.2146, -77.2811) == pytest.approx(111.2, abs=1.0)
    assert haversine_m(1.2, -77.2, 1.2, -77.2) == pytest.approx(0.0)


def test_clusters_chain_neighbours_and_respect_capacity() -> None:
    # Tres puntos en cadena (cada uno a ~110 m del siguiente) y uno lejano.
    points = [
        Located("p1", 1.2136, -77.2811),
        Located("p2", 1.2146, -77.2811),
        Located("p3", 1.2156, -77.2811),
        Located("far", 1.2500, -77.2500),
    ]
    assert cluster_by_radius(points, radius_m=150, max_size=10) == [["p1", "p2", "p3"]]
    # Con capacidad 2 no se puede formar el grupo de tres: queda un par como mucho.
    capped = cluster_by_radius(points, radius_m=150, max_size=2)
    assert all(len(group) <= 2 for group in capped)


def test_nearest_within_returns_closest_or_none() -> None:
    stops = [Located("s1", 1.2146, -77.2811), Located("s2", 1.2300, -77.2811)]
    found = nearest_within(1.2136, -77.2811, stops, radius_m=150)
    assert found is not None and found[0].ident == "s1"
    assert nearest_within(1.2136, -77.2811, stops, radius_m=50) is None
