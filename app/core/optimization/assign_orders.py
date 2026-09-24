"""Asignación determinista de pedidos por cercanía y balance de carga."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

MAX_ASSIGNABLE_ORDERS_PER_ROUTE = 50
MAX_LOAD_MULTIPLIER = 1.5
MIN_TRAFFIC_MULTIPLIER = 0.5
MAX_TRAFFIC_MULTIPLIER = 3.0
EARTH_RADIUS_METERS = 6_371_008.8


@dataclass(frozen=True, slots=True)
class Coordinates:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class CourierPosition:
    courier_id: str
    coordinates: Coordinates
    available: bool = True


@dataclass(frozen=True, slots=True)
class OrderPoint:
    order_id: str
    coordinates: Coordinates


@dataclass(frozen=True, slots=True)
class AssignmentAdjustment:
    courier_id: str
    order_id: str
    traffic_multiplier: float


@dataclass(frozen=True, slots=True)
class AssignOrdersOptions:
    adjustments: Sequence[AssignmentAdjustment] = ()
    travel_costs_minutes: Mapping[tuple[str, str], float | None] = field(default_factory=dict)


def assign_orders(
    couriers: Sequence[CourierPosition],
    orders: Sequence[OrderPoint],
    options: AssignOrdersOptions | None = None,
) -> dict[str, list[str]]:
    """Asigna por distancia euclidiana aproximada y luego limita cargas altas.

    La distancia euclidiana sirve como clustering barato; consultar el motor
    vial por cada pareja haría N*M solicitudes y no mejora esta primera pasada.
    """
    _validate_unique_ids(couriers, orders)
    _validate_coordinates(couriers, orders)
    if options is None:
        options = AssignOrdersOptions()
    available = [courier for courier in couriers if courier.available]
    assignments: dict[str, list[str]] = {courier.courier_id: [] for courier in available}
    if not available or not orders:
        return assignments

    adjustment_map = _build_adjustment_map(options.adjustments)
    travel_costs = _validate_travel_costs(options.travel_costs_minutes, couriers, orders)
    orders_by_id = {order.order_id: order for order in orders}

