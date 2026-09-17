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
