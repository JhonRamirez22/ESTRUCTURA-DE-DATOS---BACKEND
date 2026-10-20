"""Siembra datos sintéticos de demostración. Sin --apply solo muestra qué haría.

Uso:  DEMO_DISPATCHER_PASSWORD=... DEMO_COURIER_PASSWORD=... python scripts/seed_demo.py --apply
Las contraseñas (mínimo 12 caracteres) se leen del entorno; nunca van en el código.
"""

import argparse
import asyncio
import os
from datetime import UTC, datetime

from app.db.session import get_session_factory
from app.services.demo_data import DEMO_COURIERS, DEMO_ORDERS, seed_demo


async def main(apply: bool) -> None:
    if not apply:
        print(
            f"Simulación: crearía {len(DEMO_COURIERS)} repartidores y {len(DEMO_ORDERS)} pedidos "
            "ficticios. Agrega --apply para escribir en la base de datos."
        )
        return
    async with get_session_factory()() as session:
        summary = await seed_demo(
            session,
            now=datetime.now(UTC).replace(tzinfo=None),
            dispatcher_password=os.environ.get("DEMO_DISPATCHER_PASSWORD"),
            courier_password=os.environ.get("DEMO_COURIER_PASSWORD"),
        )
    print(
        f"Listo: {summary.couriers} repartidores, {summary.orders_created} pedidos nuevos, "
        f"{summary.accounts} cuentas."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
