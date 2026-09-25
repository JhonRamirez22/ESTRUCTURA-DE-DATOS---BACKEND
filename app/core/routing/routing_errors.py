"""Clasificación común de errores para decidir fallbacks sin ocultar fallos."""

from __future__ import annotations


class RoutingClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.response_body = response_body


class RecoverableRoutingError:
    """Marca rate limits/timeouts donde el caller puede usar un fallback."""


def is_routing_client_error(error: object) -> bool:
    return isinstance(error, RoutingClientError)


def is_recoverable_routing_error(error: object) -> bool:
    return isinstance(error, RecoverableRoutingError)
