"""Resultados y reconstrucción compartida de caminos."""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PathResult[Id: Hashable]:
    found: bool
    path: tuple[Id, ...]
    total_cost: float


def no_path[Id: Hashable]() -> PathResult[Id]:
    return PathResult(found=False, path=(), total_cost=math.inf)


def build_path[Id: Hashable](
    source: Id,
    target: Id,
    previous: Mapping[Id, Id],
    total_cost: float,
) -> PathResult[Id]:
    path = [target]
    current = target

    while current != source:
        if current not in previous:
            return no_path()
        predecessor = previous[current]
        path.append(predecessor)
        current = predecessor

    path.reverse()
    return PathResult(found=True, path=tuple(path), total_cost=total_cost)
