# Rutas Pasto - Backend

API FastAPI para la gestión de pedidos y la optimización de rutas de entrega en
Pasto, desarrollada como proyecto de la asignatura **Estructura de Datos**.

## Stack

FastAPI - SQLAlchemy (async) - PostgreSQL - Alembic - uv - Pydantic

## Funcionalidades

- Estructuras de datos propias: listas enlazadas, pila, cola y montículo mínimo
- Grafo de la malla vial con Dijkstra y A* sobre el área de Pasto
- Optimización de rutas con vecino más cercano y 2-opt
- Asignación de pedidos y repartidores con balanceo de carga
- Integración con proveedores de ruteo (OSRM y OpenRouteService) y tráfico (TomTom)
- Estimación de tiempos de llegada y métricas por ruta
- Notificaciones al cliente por correo y SMS con cola de reintentos
- Chat del cliente, sesiones revocables y límites de tasa

## Requisitos

- Python 3.12+ (el proyecto fija su versión en `.python-version`)
- uv
- PostgreSQL

## Puesta en marcha

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

## Pruebas

```bash
uv run pytest
uv run ruff check .
uv run mypy app
```

## Estructura

```
app/api            endpoints FastAPI
app/core           estructuras de datos, grafos, optimización, ruteo y tráfico
app/db             sesiones y base declarativa
app/models         modelos ORM
app/repositories   acceso a datos
app/schemas        contratos de la API
app/services       lógica de dominio
alembic            migraciones de base de datos
scripts            utilidades de operación
tests              pruebas unitarias e integración
```

## Equipo

- @JhonRamirez22
- @miguelcebing
- @sthebanHV
