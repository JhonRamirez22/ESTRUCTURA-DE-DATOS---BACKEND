"""Validación Web Mercator: no permite usar el proxy para consultar otras ciudades."""

import math

from app.core.location.pasto_area import (
    MAX_LATITUDE,
    MAX_LONGITUDE,
    MIN_LATITUDE,
    MIN_LONGITUDE,
)

MIN_TRAFFIC_TILE_ZOOM = 11
MAX_TRAFFIC_TILE_ZOOM = 18


def validate_traffic_tile(z: int, x: int, y: int) -> None:
    if any(type(value) is not int for value in (z, x, y)):
        raise ValueError("La tesela debe usar índices enteros.")
    if not MIN_TRAFFIC_TILE_ZOOM <= z <= MAX_TRAFFIC_TILE_ZOOM:
        raise ValueError("El zoom de tráfico debe estar entre 11 y 18.")
    size = 2**z
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError("Los índices de la tesela están fuera del rango permitido.")
    west, east = x / size * 360 - 180, (x + 1) / size * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / size))))
    # Se permite intersección: las teselas del borde no caben completamente dentro de Pasto.
    if not (
        west < MAX_LONGITUDE
        and east > MIN_LONGITUDE
        and south < MAX_LATITUDE
        and north > MIN_LATITUDE
    ):
        raise ValueError("La tesela de tráfico está fuera de Pasto.")
