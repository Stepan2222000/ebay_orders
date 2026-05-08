"""Asyncpg pool. Открывается лениво. JSONB ↔ dict декодируется автоматически."""
import json

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    for tname in ("jsonb", "json"):
        await conn.set_type_codec(
            tname,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=settings.pg_host,
            port=settings.pg_port,
            user=settings.pg_user,
            password=settings.pg_password,
            database=settings.pg_database,
            min_size=1,
            max_size=40,
            init=_init_conn,
        )
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
