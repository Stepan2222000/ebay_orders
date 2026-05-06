import json

import asyncpg

from app.config import settings


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def make_pool(min_size: int = 2, max_size: int = 20) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=settings.PGHOST,
        port=settings.PGPORT,
        user=settings.PGUSER,
        password=settings.POSTGRES_PASSWORD,
        database=settings.PGDATABASE,
        min_size=min_size,
        max_size=max_size,
        init=_init_conn,
    )
