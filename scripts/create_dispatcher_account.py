"""Crea despacho por consola; no hay registro público ni contraseña predeterminada."""

import asyncio
from getpass import getpass

from pydantic import ValidationError
from sqlalchemy import select

from app.core.passwords import hash_password
from app.db.session import get_session_factory
from app.models import Account
from app.schemas.auth import AccountCredentials


async def create_dispatcher(credentials: AccountCredentials) -> None:
    async with get_session_factory()() as session:
        existing = await session.scalar(
            select(Account.id).where(Account.email == credentials.email)
        )
        if existing is not None:
            raise ValueError("El correo ya está registrado; no se sobrescribió su cuenta.")
        session.add(
            Account(
                email=credentials.email,
                password_hash=hash_password(credentials.password.get_secret_value()),
                role="dispatcher",
            )
        )
        await session.commit()


if __name__ == "__main__":
    email = input("Correo del despachador: ")
    password = getpass("Contraseña (mínimo 15 caracteres): ")
    if password != getpass("Confirmar contraseña: "):
        raise SystemExit("Las contraseñas no coinciden.")
    try:
        credentials = AccountCredentials.model_validate({"email": email, "password": password})
    except ValidationError:
        raise SystemExit(
            "Ingresa un correo válido y una contraseña de 15 a 128 caracteres."
        ) from None
    asyncio.run(create_dispatcher(credentials))
