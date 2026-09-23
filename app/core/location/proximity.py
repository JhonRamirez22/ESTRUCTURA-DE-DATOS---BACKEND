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
