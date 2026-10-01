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

    def __len__(self) -> int:
        return len(self._where)

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._where

    def nearest(
        self,
        point: GeoPoint,
        k: int,
        *,
        max_metres: float = math.inf,
        exclude: Id | None = None,
    ) -> list[tuple[Id, float]]:
        """Up to ``k`` closest items as ``(id, metres)`` sorted by distance, then by id."""
        if k < 1 or not self._where:
            return []
        origin = self._cell_of(point)
        found: list[tuple[float, str, Id]] = []
        max_ring = int(min(max_metres / self._cell, 10_000)) + 1
        ring = 0
        while ring <= max_ring:
            for cell in self._ring_cells(origin, ring):
                for item_id, item_point in self._cells.get(cell, {}).items():
                    if item_id == exclude:
                        continue
                    distance = point.distance_to(item_point)
                    if distance <= max_metres:
                        found.append((distance, repr(item_id), item_id))
            found.sort(key=lambda entry: (entry[0], entry[1]))
            del found[k:]
            # Anything in the next ring is at least ``ring * cell`` metres away.
            if len(found) == k and found[-1][0] <= ring * self._cell:
                break
            if len(found) >= len(self._where) - (1 if exclude in self._where else 0):
                break
            ring += 1
        return [(item_id, distance) for distance, _, item_id in found]

    @staticmethod
    def _ring_cells(origin: tuple[int, int], ring: int) -> list[tuple[int, int]]:
        ox, oy = origin
        if ring == 0:
            return [origin]
        cells = [(ox + dx, oy - ring) for dx in range(-ring, ring + 1)]
        cells += [(ox + dx, oy + ring) for dx in range(-ring, ring + 1)]
        cells += [(ox - ring, oy + dy) for dy in range(-ring + 1, ring)]
        cells += [(ox + ring, oy + dy) for dy in range(-ring + 1, ring)]
        return cells
