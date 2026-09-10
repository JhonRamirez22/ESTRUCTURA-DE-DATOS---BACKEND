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

## Cuándo se optimiza

- `POST /api/v1/asignaciones` propone repartidores por costos viales y balanceo:
  no consulta Groq/TomTom ni crea rutas.
- Al confirmar `POST /api/v1/asignaciones/aplicar`, OSRM/ORS calcula calles,
  TomTom muestrea tráfico y Groq asesora sobre esos datos. Vecino más cercano
  y 2-opt ordenan las paradas; la ruta se guarda de forma transaccional.
- Consultar rutas o navegación no vuelve a optimizarlas. El recálculo es explícito;
  un repartidor solo puede recalcular sus paradas asignadas, nunca añadir pedidos.
- La capa visual se actualiza cada 90 segundos con rutas activas y pestaña visible,
  sin modificar el despacho. Datos antiguos y falta de cobertura se indican;
  el tráfico puede mantenerse igual si el proveedor no reporta cambios.
- Las mediciones TomTom y los cierres prevalecen sobre sugerencias de IA.
  Si un proveedor falla, se informa el alcance; nunca se dibuja una línea recta
  como sustituto de un recorrido vial.
- La comparación antes/después utiliza la misma matriz ajustada. Si el orden previo
  queda incomunicado, no se presenta un ahorro infinito ni se descarta una alternativa válida.
- Errores HTTP y excepciones incluyen `X-Request-ID`; CloudWatch permite correlacionarlos
  con plantillas de endpoints, sin registrar direcciones, guías o secretos.

## Requisitos
