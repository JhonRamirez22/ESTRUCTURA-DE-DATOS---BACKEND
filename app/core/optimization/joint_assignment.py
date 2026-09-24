"""Mejora conjunta del reparto: mueve pedidos entre repartidores para acortar las rutas.

``assign_orders`` reparte por cercanía al repartidor y balancea cargas, pero mira cada pedido por
separado: dos pedidos vecinos pueden acabar en rutas distintas aunque una sola pasada por ambos
costara casi nada. Aquí se parte de ese reparto y se aplican movimientos (trasladar un pedido o
intercambiar dos) mientras bajen el costo conjunto:

    objetivo = minutos totales de todas las rutas + peso · minutos de la ruta más larga

El segundo término evita «ahorrar» cargando a un solo repartidor. Cada movimiento se evalúa con el
costo estimado de la ruta de cada repartidor sobre la misma matriz vial (vecino más cercano más
búsqueda local, ver ``refine``); un ``MinHeap`` ordena los movimientos candidatos y se aplica el
que más baja el objetivo. Nunca empeora el reparto de partida.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.core.optimization.nearest_neighbor import nearest_neighbor
from app.core.optimization.refine import local_search, route_cost
from app.core.structures.min_heap import MinHeap

MAKESPAN_WEIGHT = 0.5
MAX_PASSES = 25
MAX_EVALUATIONS = 4_000
MAX_ORDERS_PER_COURIER = 50
# Mismo criterio de balance que ``assign_orders``: nadie recibe más de 1,5 veces la carga media.
LOAD_CAP_MULTIPLIER = 1.5
MIN_GAIN_MINUTES = 0.05
NEIGHBOURS_FOR_SWAPS = 6

Matrix = Sequence[Sequence[float | None]]


@dataclass(frozen=True, slots=True)
class AssignmentCost:
    total_minutes: float
    longest_route_minutes: float

    def objective(self, weight: float = MAKESPAN_WEIGHT) -> float:
        return self.total_minutes + weight * self.longest_route_minutes


@dataclass(frozen=True, slots=True)
class Move:
    """Un movimiento aplicado: ``order_id`` pasó de ``from_courier`` a ``to_courier``."""

    kind: str  # "relocate" | "swap"
    order_id: str
    from_courier: str
    to_courier: str
    swapped_order_id: str | None
    gain: float  # reducción del objetivo, en minutos


@dataclass(frozen=True, slots=True)
class JointAssignmentResult:
    assignments: dict[str, list[str]]
    before: AssignmentCost
    after: AssignmentCost
    moves: list[Move]
    evaluations: int

