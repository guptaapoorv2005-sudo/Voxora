"""Provision the single owner interactively; never pass passwords on the command line."""

import asyncio
import getpass

from sqlalchemy import select, text

from app.api.routes_auth import password_hash
from app.db.models.owner import OwnerUser
from app.db.session import dispose_engine, get_session_factory


async def provision(email: str, password: str) -> None:
    async with get_session_factory()() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(18273645, 8)"))
        if (await db.execute(select(OwnerUser))).scalars().first() is not None:
            raise RuntimeError("An owner already exists; provisioning did not change it")
        db.add(OwnerUser(email=email, hashed_password=password_hash.hash(password), is_active=True))
        await db.commit()
    await dispose_engine()


def main() -> None:
    email = input("Owner email: ").strip().lower()
    password = getpass.getpass("Owner password (at least 12 characters): ")
    confirm = getpass.getpass("Confirm password: ")
    if (
        not 3 <= len(email) <= 255
        or "@" not in email
        or len(password) < 12
        or len(password) > 1024
        or password != confirm
    ):
        raise SystemExit("Invalid email or passwords do not match the length requirements")
    asyncio.run(provision(email, password))
    print("Owner created.")


if __name__ == "__main__":
    main()
