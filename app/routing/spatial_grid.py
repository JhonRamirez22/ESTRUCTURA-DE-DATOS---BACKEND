"""Uniform spatial hash for fast nearest-neighbour queries over delivery stops."""

from __future__ import annotations

import math
from collections.abc import Hashable

from app.routing.domain import GeoPoint

_METRES_PER_DEGREE = 111_320.0


class SpatialGrid[Id: Hashable]:
    """Buckets points into square cells so a query only scans nearby cells.

    Inserting and removing are O(1); a nearest query scans rings of cells outward and stops as
    soon as the ``k``-th best distance is closer than the next ring, so it costs O(k) for the
    evenly spread stops of one city instead of O(n).
    """

    def __init__(self, cell_metres: float = 400.0, reference_lat: float = 1.2136) -> None:
        if cell_metres <= 0:
            raise ValueError("cell_metres must be positive.")
        self._cell = cell_metres
        self._cos_lat = math.cos(math.radians(reference_lat))
        self._cells: dict[tuple[int, int], dict[Id, GeoPoint]] = {}
        self._where: dict[Id, tuple[int, int]] = {}

    def _project(self, point: GeoPoint) -> tuple[float, float]:
        return (
            point.lng * _METRES_PER_DEGREE * self._cos_lat,
            point.lat * _METRES_PER_DEGREE,
        )

    def _cell_of(self, point: GeoPoint) -> tuple[int, int]:
        x, y = self._project(point)
        return (math.floor(x / self._cell), math.floor(y / self._cell))

    def insert(self, item_id: Id, point: GeoPoint) -> None:
        if item_id in self._where:
            self.remove(item_id)
        cell = self._cell_of(point)
        self._cells.setdefault(cell, {})[item_id] = point
        self._where[item_id] = cell

    def remove(self, item_id: Id) -> None:
        cell = self._where.pop(item_id)
        bucket = self._cells[cell]
        del bucket[item_id]
        if not bucket:
            del self._cells[cell]
