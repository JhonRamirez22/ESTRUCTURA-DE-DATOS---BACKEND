"""Live re-optimisation of a whole fleet: better order per route plus stop moves between routes.

The re-optimiser never changes anything by itself: it returns a :class:`FleetProposal` that
the dispatcher can accept or ignore. A stop the courier is already driving to is *locked* and
stays first in its route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.core.structures.min_heap import MinHeap
from app.routing.cost_matrix import CostMatrix
from app.routing.domain import Fleet, FleetIndex
from app.routing.scoreboard import PlanScore, PlanScoreboard
from app.routing.solvers import RoutePipeline
from app.routing.spatial_grid import SpatialGrid

MAKESPAN_WEIGHT = 0.5
LOAD_CAP_MULTIPLIER = 1.5
MIN_MOVE_GAIN_MINUTES = 0.5
MIN_REORDER_GAIN_MINUTES = 0.2
MAX_MOVES = 6
NEIGHBOUR_COUNT = 4
NEIGHBOUR_RADIUS_METRES = 2_500.0


@dataclass(frozen=True, slots=True)
class StopMove:
    """One stop proposed to change courier."""

    stop_id: str
    from_route: str
    to_route: str
    gain_minutes: float


@dataclass(frozen=True, slots=True)
class RouteProposal:
    route_id: str
    current_order: tuple[str, ...]
    proposed_order: tuple[str, ...]
    current_minutes: float
    proposed_minutes: float

    @property
    def saved_minutes(self) -> float:
        if not (math.isfinite(self.current_minutes) and math.isfinite(self.proposed_minutes)):
            return 0.0
        return self.current_minutes - self.proposed_minutes

    @property
    def changed(self) -> bool:
        return self.current_order != self.proposed_order

    @property
    def moved_in(self) -> tuple[str, ...]:
        return tuple(s for s in self.proposed_order if s not in self.current_order)

    @property
    def moved_out(self) -> tuple[str, ...]:
        return tuple(s for s in self.current_order if s not in self.proposed_order)


@dataclass(frozen=True, slots=True)
class FleetProposal:
    routes: tuple[RouteProposal, ...]
    moves: tuple[StopMove, ...]
    score_before: PlanScore
    score_after: PlanScore

    @property
    def changed(self) -> bool:
        return any(route.changed for route in self.routes)

    @property
    def saved_minutes(self) -> float:
        return self.score_before.total_minutes - self.score_after.total_minutes


class RouteImprover:
    """Improves one route's order, keeping the locked prefix and the courier origin fixed."""

    def __init__(self, matrix: CostMatrix, pipeline: RoutePipeline | None = None) -> None:
        self._matrix = matrix
        self._pipeline = pipeline or RoutePipeline.standard()

    def improve(self, courier_node: int, stop_nodes: list[int], locked: int) -> list[int]:
        if len(stop_nodes) - locked < 2:
            return list(stop_nodes)
        prefix = stop_nodes[:locked]
        origin = prefix[-1] if prefix else courier_node
        movable = stop_nodes[locked:]
        result = self._pipeline.improve(self._matrix, [origin, *movable])
        return prefix + result.order[1:]


class _RouteState:
    """Mutable working copy of one route used while searching for stop moves."""

    def __init__(self, route_id: str, courier_node: int, order: list[int], locked: int) -> None:
        self.route_id = route_id
        self.courier_node = courier_node
        self.order = order
        self.locked = locked

    def path(self) -> list[int]:
        return [self.courier_node, *self.order]

    def minutes(self, matrix: CostMatrix) -> float:
        return matrix.path_cost(self.path())

    def removal_delta(self, matrix: CostMatrix, position: int) -> float:
        """Minutes saved by removing the stop at ``position`` of ``order``."""
        path = self.path()
        previous, node = path[position], path[position + 1]
        saved = matrix.cost(previous, node)
        if position + 2 < len(path):
            following = path[position + 2]
            saved += matrix.cost(node, following) - matrix.cost(previous, following)
        return saved

    def best_insertion(self, matrix: CostMatrix, node: int) -> tuple[int, float]:
        """Cheapest ``(position, extra_minutes)`` to insert ``node`` after the locked prefix."""
        path = self.path()
        best_position, best_extra = self.locked, math.inf
        for position in range(self.locked, len(self.order) + 1):
            previous = path[position]
            extra = matrix.cost(previous, node)
            if position + 1 < len(path):
                following = path[position + 1]
                extra += matrix.cost(node, following) - matrix.cost(previous, following)
            if extra < best_extra:
                best_position, best_extra = position, extra
        return best_position, best_extra


@dataclass(frozen=True, slots=True)
class _Candidate:
    gain: float
    key: tuple[int, str]
    node: int
    source: str
    target: str
    remove_position: int
    insert_position: int
    remove_delta: float
    insert_delta: float


def _compare(left: _Candidate, right: _Candidate) -> int:
    if left.gain != right.gain:
        return -1 if left.gain > right.gain else 1  # larger gain first
    return (left.key > right.key) - (left.key < right.key)


class CrossRouteBalancer:
    """Greedy best-gain relocation of stops between routes.

    A :class:`SpatialGrid` limits each stop to the routes that own one of its nearest
    neighbours, and a :class:`MinHeap` ranks the candidate moves so the best is applied first.
    """

    def __init__(
        self,
        matrix: CostMatrix,
        index: FleetIndex,
        *,
        makespan_weight: float = MAKESPAN_WEIGHT,
        max_moves: int = MAX_MOVES,
        min_gain: float = MIN_MOVE_GAIN_MINUTES,
    ) -> None:
        self._matrix = matrix
        self._index = index
        self._weight = makespan_weight
        self._max_moves = max_moves
        self._min_gain = min_gain

    def rebalance(self, states: dict[str, _RouteState]) -> list[StopMove]:
        node_to_stop = {node: stop_id for stop_id, node in self._index.stop_node.items()}
        owner = {node: route_id for route_id, st in states.items() for node in st.order}
        grid: SpatialGrid[int] = SpatialGrid()
        for node in owner:
            grid.insert(node, self._index.points[node])
        total_stops = len(owner)
        cap = max(
            1,
            math.floor(total_stops / max(1, len(states)) * LOAD_CAP_MULTIPLIER),
            max((len(st.order) for st in states.values()), default=0),
        )
        moves: list[StopMove] = []
        for _ in range(self._max_moves):
            best = self._best_candidate(states, owner, grid, cap)
            if best is None or best.gain < self._min_gain:
                break
            source, target = states[best.source], states[best.target]
            source.order.pop(best.remove_position)
            target.order.insert(best.insert_position, best.node)
            owner[best.node] = best.target
            moves.append(StopMove(node_to_stop[best.node], best.source, best.target, best.gain))
        return moves

    def _objective(self, costs: dict[str, float]) -> float:
        finite = [value for value in costs.values() if math.isfinite(value)]
        return sum(finite) + self._weight * max(finite, default=0.0)

    def _best_candidate(
        self,
        states: dict[str, _RouteState],
        owner: dict[int, str],
        grid: SpatialGrid[int],
        cap: int,
    ) -> _Candidate | None:
        matrix = self._matrix
        costs = {route_id: st.minutes(matrix) for route_id, st in states.items()}
        base = self._objective(costs)
        heap: MinHeap[_Candidate, tuple[int, str]] = MinHeap(_compare, lambda item: item.key)
        for source_id, source in states.items():
            if len(source.order) <= 1:
                continue  # never leave a courier without stops
            for position in range(source.locked, len(source.order)):
