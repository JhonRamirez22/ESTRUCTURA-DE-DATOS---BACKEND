"""Semilla E2E acotada a una base local; nunca apunta a producción."""

import asyncio
import os

from sqlalchemy import select

from app.core.passwords import hash_password
from app.db.session import get_session_factory
from app.models import Account
from app.settings import get_settings


async def seed() -> None:
    settings = get_settings()
    url = settings.sqlalchemy_database_url
    if os.environ.get("NODE_ENV") != "test" or url.host not in {None, "localhost", "127.0.0.1"}:
        raise RuntimeError("La semilla E2E requiere NODE_ENV=test y una base local.")
    email = "dispatcher@rutas-pasto.example"
    password = "rutas-pasto-e2e-private-password"
    async with get_session_factory()() as session:
        if await session.scalar(select(Account.id).where(Account.email == email)) is None:
            session.add(
                Account(email=email, password_hash=hash_password(password), role="dispatcher")
            )
            await session.commit()


if __name__ == "__main__":
    asyncio.run(seed())
