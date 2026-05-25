"""Идемпотентный postgres_fdw-сетап в БД ebay_orders.

Создаёт foreign-серверы к каталогу `smart` (5402) и справочнику брендов/правил
`brands_mapping` (5411) и импортирует нужные таблицы в схемы `smart_fdw` /
`brands_fdw`. Пароль берётся из окружения (POSTGRES_PASSWORD) — в коммите его нет.

Все три БД на одном хосте с одним admin/паролем (см. app.config). Запуск из корня
проекта: `python db/setup_fdw.py`. Повторный запуск безопасен (DROP SERVER CASCADE
+ пересоздание).
"""
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

_SMART_PORT = 5402
_BRANDS_PORT = 5411


def _ddl(host: str, user: str, pw: str) -> str:
    pw = pw.replace("'", "''")
    return f"""
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;

    DROP SERVER IF EXISTS smart_srv  CASCADE;
    DROP SERVER IF EXISTS brands_srv CASCADE;

    CREATE SERVER smart_srv  FOREIGN DATA WRAPPER postgres_fdw
        OPTIONS (host '{host}', port '{_SMART_PORT}',  dbname 'smart');
    CREATE SERVER brands_srv FOREIGN DATA WRAPPER postgres_fdw
        OPTIONS (host '{host}', port '{_BRANDS_PORT}', dbname 'brands_mapping');

    CREATE USER MAPPING FOR CURRENT_USER SERVER smart_srv
        OPTIONS (user '{user}', password '{pw}');
    CREATE USER MAPPING FOR CURRENT_USER SERVER brands_srv
        OPTIONS (user '{user}', password '{pw}');

    CREATE SCHEMA IF NOT EXISTS smart_fdw;
    CREATE SCHEMA IF NOT EXISTS brands_fdw;

    IMPORT FOREIGN SCHEMA public LIMIT TO (parts, part_articles)
        FROM SERVER smart_srv INTO smart_fdw;
    IMPORT FOREIGN SCHEMA public LIMIT TO (article_match_rules, brands, brand_aliases)
        FROM SERVER brands_srv INTO brands_fdw;
    """


async def main() -> None:
    conn = await asyncpg.connect(
        host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
        password=settings.pg_password, database=settings.pg_database,
    )
    try:
        async with conn.transaction():
            await conn.execute(_ddl(settings.pg_host, settings.pg_user, settings.pg_password))
        n_art = await conn.fetchval("SELECT count(*) FROM smart_fdw.part_articles")
        n_rules = await conn.fetchval("SELECT count(*) FROM brands_fdw.article_match_rules")
        print(f"FDW ready: smart_fdw.part_articles={n_art}, brands_fdw.article_match_rules={n_rules}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
