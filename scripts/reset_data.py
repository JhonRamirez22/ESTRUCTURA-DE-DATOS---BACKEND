"""Borra los datos operativos (pedidos, rutas, repartidores, eventos). IRREVERSIBLE.

Conserva cuentas, sesiones y auditoría. Sin --confirm solo cuenta lo que se borraría.
Para borrar de verdad exige además RESET_CONFIRM=BORRAR-DATOS-OPERATIVOS en el entorno.
Haz una copia (snapshot de RDS) antes de ejecutarlo en producción.
"""

import argparse
import asyncio
import os

from app.db.session import get_session_factory
from app.services.demo_data import count_operational_data, reset_operational_data

REQUIRED_CONFIRMATION = "BORRAR-DATOS-OPERATIVOS"


async def main(confirm: bool) -> None:
    async with get_session_factory()() as session:
        counts = await count_operational_data(session)
        print("Datos operativos actuales:", counts)
        if not confirm:
            print("Simulación: no se borró nada. Usa --confirm y RESET_CONFIRM para borrar.")
            return
        if os.environ.get("RESET_CONFIRM") != REQUIRED_CONFIRMATION:
            raise SystemExit(f"Falta RESET_CONFIRM={REQUIRED_CONFIRMATION}; no se borró nada.")
        await reset_operational_data(session)
        print("Datos operativos borrados:", counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    asyncio.run(main(parser.parse_args().confirm))
