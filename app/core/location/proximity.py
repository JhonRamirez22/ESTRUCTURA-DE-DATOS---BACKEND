"""Cercanía entre ubicaciones: distancia en metros y agrupamiento por radio.

Puro y sin I/O: lo usan la agrupación de pedidos y el aviso de paradas cercanas.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.structures.disjoint_set import DisjointSet

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat_a: float, lng_a: float, lat_b: float, lng_b: float) -> float:
    """Distancia de círculo máximo en metros entre dos coordenadas [lat, lng]."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lng_b - lng_a)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True, slots=True)
class Located:
    ident: str
    lat: float
    lng: float


def cluster_by_radius(points: Sequence[Located], radius_m: float, max_size: int) -> list[list[str]]:
    """Agrupa puntos que se alcanzan en cadena dentro de `radius_m` (enlace simple).

    Dos puntos se unen si están a menos de `radius_m`; una unión que superaría `max_size`
    se omite para que ningún grupo exceda la capacidad de un repartidor. Devuelve solo
    grupos de al menos dos puntos, ordenados por tamaño descendente.
    """
    if radius_m <= 0 or max_size < 2:
        return []
    ds: DisjointSet[str] = DisjointSet([point.ident for point in points])
    pairs: list[tuple[float, int, int]] = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            distance = haversine_m(points[i].lat, points[i].lng, points[j].lat, points[j].lng)
            if distance < radius_m:
                pairs.append((distance, i, j))
    # Los pares más cercanos primero: así los grupos crecen alrededor de los vecinos reales.
    pairs.sort()
    for _distance, i, j in pairs:
        left, right = points[i].ident, points[j].ident
        if ds.find(left) == ds.find(right):
            continue
        if ds.size_of(left) + ds.size_of(right) > max_size:
            continue
        ds.union(left, right)
    groups = [sorted(group) for group in ds.groups() if len(group) >= 2]
    return sorted(groups, key=lambda group: (-len(group), group))


def nearest_within(
    lat: float, lng: float, candidates: Sequence[Located], radius_m: float
) -> tuple[Located, float] | None:
    """Punto candidato más cercano dentro de `radius_m`, o None."""
    best: tuple[Located, float] | None = None
    for candidate in candidates:
        distance = haversine_m(lat, lng, candidate.lat, candidate.lng)
        if distance < radius_m and (best is None or distance < best[1]):
            best = (candidate, distance)
    return best
