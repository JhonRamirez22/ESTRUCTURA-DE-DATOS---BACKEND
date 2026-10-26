from __future__ import annotations

import re

from app.main import app

_EXPECTED_OPERATIONS = frozenset(
    {
        ("GET", "/api/analitica"),
        ("POST", "/api/asignaciones"),
        ("POST", "/api/asignaciones/aplicar"),
        ("GET", "/api/auth/access"),
        ("GET", "/api/auth/session"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("PUT", "/api/auth/courier-account"),
        ("POST", "/api/cliente/chat"),
        ("GET", "/api/cliente/chat/estado"),
        ("GET", "/api/health"),
        ("POST", "/api/direcciones/resolver"),
        ("GET", "/api/geocoding/search"),
        ("GET", "/api/notificaciones"),
        ("GET", "/api/notificaciones/clientes/estado"),
        ("GET", "/api/optimizaciones/recientes"),
        ("POST", "/api/optimizaciones/resumen"),
        ("GET", "/api/pedidos"),
        ("POST", "/api/pedidos"),
        ("DELETE", "/api/pedidos/{}"),
        ("GET", "/api/pedidos/{}"),
        ("PATCH", "/api/pedidos/{}"),
        ("GET", "/api/repartidores"),
        ("POST", "/api/repartidores"),
        ("GET", "/api/repartidores/me"),
        ("DELETE", "/api/repartidores/me/ubicacion"),
        ("POST", "/api/repartidores/me/ubicacion"),
        ("DELETE", "/api/repartidores/{}"),
        ("GET", "/api/repartidores/{}"),
        ("PATCH", "/api/repartidores/{}"),
        ("POST", "/api/repartidores/{}/codigo"),
        ("DELETE", "/api/repartidores/{}/ubicacion"),
        ("POST", "/api/repartidores/{}/ubicacion"),
        ("GET", "/api/rutas"),
        ("POST", "/api/rutas"),
        ("GET", "/api/rutas/{}"),
        ("PATCH", "/api/rutas/{}"),
        ("GET", "/api/rutas/{}/eta"),
        ("GET", "/api/rutas/{}/navegacion"),
        ("POST", "/api/rutas/{}/recalcular"),
        ("POST", "/api/rutas/{}/propuesta"),
        ("POST", "/api/rutas/{}/deshacer-recalculo"),
        ("GET", "/api/rutas/{}/trafico"),
        ("GET", "/api/rutas/{}/trafico-vivo"),
        ("GET", "/api/seguimiento"),
        ("POST", "/api/solicitudes"),
        ("POST", "/api/solicitudes/direccion"),
        ("GET", "/api/inteligencia/agrupaciones"),
        ("POST", "/api/inteligencia/rutas/{}/explicar"),
        ("POST", "/api/solicitudes/{}/aceptar"),
        ("POST", "/api/solicitudes/{}/rechazar"),
        ("GET", "/api/traffic"),
        ("GET", "/api/trafico/estado"),
        ("GET", "/api/trafico/tiles/{}/{}/{}.png"),
    }
)
_DYNAMIC_SEGMENT = re.compile(r"\{[^/]+\}")


def test_fastapi_implements_the_complete_http_api_contract() -> None:
    actual_operations = {
        (method.upper(), _DYNAMIC_SEGMENT.sub("{}", path.replace("/api/v1/", "/api/")))
        for path, path_operations in app.openapi()["paths"].items()
        for method in path_operations
    }
    missing = sorted(_EXPECTED_OPERATIONS - actual_operations)
    unexpected = sorted(actual_operations - _EXPECTED_OPERATIONS)

    assert not missing, f"FastAPI dejó de implementar operaciones públicas: {missing}"
    assert not unexpected, f"FastAPI agregó operaciones no documentadas: {unexpected}"
    assert all(path.startswith("/api/v1/") for path in app.openapi()["paths"])


def test_openapi_documentation_is_not_published_as_a_public_route() -> None:
    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
