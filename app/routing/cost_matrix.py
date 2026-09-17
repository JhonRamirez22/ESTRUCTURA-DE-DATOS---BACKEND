"""Directed travel-time matrix where ``None`` means "no road connection", never zero."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


class CostMatrix:
    """Square matrix of minutes between nodes; wraps the rows the solvers already consume."""

    def __init__(self, rows: Sequence[Sequence[float | None]]) -> None:
        size = len(rows)
        for row in rows:
            if len(row) != size:
                raise ValueError("The cost matrix must be square.")
            for cost in row:
                if cost is not None and (not math.isfinite(cost) or cost < 0):
                    raise ValueError("Costs must be None or finite non-negative numbers.")
        self._rows: list[list[float | None]] = [list(row) for row in rows]

    @property
    def size(self) -> int:
        return len(self._rows)

    @property
    def rows(self) -> list[list[float | None]]:
        return self._rows

    def cost(self, origin: int, destination: int) -> float:
        """Minutes from origin to destination; ``math.inf`` when unreachable."""
        value = self._rows[origin][destination]
        return math.inf if value is None else value

    def path_cost(self, nodes: Sequence[int]) -> float:
        """Total minutes of an open path through ``nodes`` in order."""
        return sum(self.cost(a, b) for a, b in zip(nodes, nodes[1:], strict=False))

    def submatrix(self, nodes: Iterable[int]) -> CostMatrix:
        picked = list(nodes)
        rows = [[self._rows[a][b] if a != b else 0.0 for b in picked] for a in picked]
        return CostMatrix(rows)
