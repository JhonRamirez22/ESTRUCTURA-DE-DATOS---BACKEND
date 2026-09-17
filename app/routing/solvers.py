"""Interchangeable single-route solvers and the pipeline that chains them.

Each :class:`RouteSolver` improves an open path whose first node (the courier, or the stop the
courier is already driving to) stays fixed. The :class:`RoutePipeline` runs them in sequence
and never returns anything worse than the order it was given.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.or_opt import or_opt
from app.core.optimization.refine import refine_route
from app.core.optimization.two_opt import two_opt
from app.routing.cost_matrix import CostMatrix


@dataclass(frozen=True, slots=True)
class SolverResult:
    """A visiting order (matrix indices, origin first), its cost and who produced it."""

    order: list[int]
    cost: float
    solver: str


class RouteSolver(ABC):
    """Strategy interface: turn an order into a cheaper (or equal) one."""

    name: str = "solver"

    @abstractmethod
    def improve(self, matrix: CostMatrix, order: Sequence[int]) -> list[int]:
        """Return a permutation of ``order`` that keeps ``order[0]`` first."""


class NearestNeighborSolver(RouteSolver):
    """Greedy construction from the origin; ignores the incoming order except its first node."""

    name = "nearest-neighbor"

    def improve(self, matrix: CostMatrix, order: Sequence[int]) -> list[int]:
        nodes = list(order)
        sub = matrix.submatrix(nodes)
        return [nodes[i] for i in nearest_neighbor(sub.rows, 0)]


class TwoOptSolver(RouteSolver):
    name = "2-opt"

    def improve(self, matrix: CostMatrix, order: Sequence[int]) -> list[int]:
