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

    for order in orders:
        eligible = [
            courier
            for courier in available
            if len(assignments[courier.courier_id]) < MAX_ASSIGNABLE_ORDERS_PER_ROUTE
        ]
        if not eligible:
            break

        nearest = min(
            eligible,
            key=lambda courier: _pair_cost(
                courier,
                order,
                adjustment_map,
                travel_costs,
            ),
        )
        if not math.isfinite(
            _pair_cost(nearest, order, adjustment_map, travel_costs)
        ):
            continue
        assignments[nearest.courier_id].append(order.order_id)

    _balance_assignments(assignments, available, orders_by_id, adjustment_map, travel_costs)
    return assignments


def _balance_assignments(
    assignments: dict[str, list[str]],
    couriers: Sequence[CourierPosition],
    orders_by_id: dict[str, OrderPoint],
    adjustment_map: dict[tuple[str, str], float],
    travel_costs: Mapping[tuple[str, str], float | None],
) -> None:
    average_load = len(orders_by_id) / len(couriers)
    maximum_load = min(
        max(1, math.floor(average_load * MAX_LOAD_MULTIPLIER)),
        MAX_ASSIGNABLE_ORDERS_PER_ROUTE,
    )

    while True:
        overloaded = max(
            (
                courier
                for courier in couriers
                if len(assignments[courier.courier_id]) > maximum_load
            ),
            key=lambda courier: len(assignments[courier.courier_id]),
            default=None,
        )
        if overloaded is None:
            return

        possible_destinations = [
            (
                courier,
                [
                    order_id
                    for order_id in assignments[overloaded.courier_id]
                    if math.isfinite(
                        _pair_cost(
                            courier,
                            orders_by_id[order_id],
                            adjustment_map,
                            travel_costs,
                        )
                    )
                ],
            )
            for courier in couriers
            if courier.courier_id != overloaded.courier_id
            and len(assignments[courier.courier_id]) < maximum_load
        ]
        possible_destinations = [item for item in possible_destinations if item[1]]
        destination = min(
            possible_destinations,
            key=lambda item: len(assignments[item[0].courier_id]),
            default=None,
        )
        if destination is None:
            return
        destination_courier, movable_orders = destination

        # Mover primero el pedido más cercano al nuevo repartidor reduce el desvío.
        order_to_move = min(
            movable_orders,
            key=lambda order_id: _pair_cost(
                destination_courier,
                orders_by_id[order_id],
                adjustment_map,
                travel_costs,
            ),
            default=None,
        )
        if order_to_move is None:
            return

        assignments[overloaded.courier_id].remove(order_to_move)
        assignments[destination_courier.courier_id].append(order_to_move)


def _pair_cost(
    courier: CourierPosition,
    order: OrderPoint,
    adjustment_map: Mapping[tuple[str, str], float],
    travel_costs: Mapping[tuple[str, str], float | None],
) -> float:
    pair = (courier.courier_id, order.order_id)
    base_cost = (
        travel_costs[pair]
        if pair in travel_costs
        else _assignment_cost(courier.coordinates, order.coordinates)
    )
    if base_cost is None:
        return math.inf
    return base_cost * adjustment_map.get(pair, 1.0)


def _build_adjustment_map(
    adjustments: Sequence[AssignmentAdjustment],
) -> dict[tuple[str, str], float]:
    result: dict[tuple[str, str], float] = {}
    for adjustment in adjustments:
        multiplier = adjustment.traffic_multiplier
        if (
            not math.isfinite(multiplier)
            or not MIN_TRAFFIC_MULTIPLIER <= multiplier <= MAX_TRAFFIC_MULTIPLIER
        ):
            raise ValueError(
                "traffic_multiplier debe estar entre "
                f"{MIN_TRAFFIC_MULTIPLIER} y {MAX_TRAFFIC_MULTIPLIER}"
            )

        key = (adjustment.courier_id, adjustment.order_id)
        if key in result:
            raise ValueError(
                "No puede haber más de un ajuste para "
                f"{adjustment.courier_id}/{adjustment.order_id}"
            )
        result[key] = multiplier
    return result


def _assignment_cost(
    courier_coordinates: Coordinates,
    order_coordinates: Coordinates,
) -> float:
    """Usa distancia geodésica solo para pruebas/compatibilidad sin matriz vial."""
    latitude_delta = math.radians(order_coordinates.lat - courier_coordinates.lat)
    longitude_delta = math.radians(order_coordinates.lng - courier_coordinates.lng)
    start_latitude = math.radians(courier_coordinates.lat)
    end_latitude = math.radians(order_coordinates.lat)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(start_latitude)
        * math.cos(end_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.atan2(
        math.sqrt(haversine), math.sqrt(1 - haversine)
    )


def _validate_travel_costs(
    travel_costs: Mapping[tuple[str, str], float | None],
    couriers: Sequence[CourierPosition],
    orders: Sequence[OrderPoint],
) -> dict[tuple[str, str], float | None]:
    valid_pairs = {
        (courier.courier_id, order.order_id)
        for courier in couriers
        for order in orders
    }
    result = dict(travel_costs)
    for pair, cost in result.items():
        if pair not in valid_pairs:
            raise ValueError("travel_costs_minutes contiene un par de courier/pedido desconocido")
        if cost is not None and (not math.isfinite(cost) or cost < 0):
            raise ValueError("travel_costs_minutes debe contener minutos no negativos o None")
    return result


def _validate_unique_ids(
    couriers: Sequence[CourierPosition],
    orders: Sequence[OrderPoint],
) -> None:
    if len({courier.courier_id for courier in couriers}) != len(couriers):
        raise ValueError("Los courier_id deben ser únicos")
    if len({order.order_id for order in orders}) != len(orders):
        raise ValueError("Los order_id deben ser únicos")


def _validate_coordinates(
    couriers: Sequence[CourierPosition],
    orders: Sequence[OrderPoint],
) -> None:
    for coordinates in [courier.coordinates for courier in couriers] + [
        order.coordinates for order in orders
    ]:
        if (
            not math.isfinite(coordinates.lat)
            or not math.isfinite(coordinates.lng)
            or not -90 <= coordinates.lat <= 90
            or not -180 <= coordinates.lng <= 180
        ):
            raise ValueError("Las coordenadas deben ser lat/lng finitas dentro del rango global")
