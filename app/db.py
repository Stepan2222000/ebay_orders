"""Asyncpg pool. Открывается лениво. JSONB ↔ dict декодируется автоматически.

Также: dump_schema_text(conn) — компактный текстовый снимок public-схемы для
системного промпта стадии B (чтобы модель не угадывала имена колонок и FK).
"""
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


# ─── DDL-снимок для системного промпта ──────────────────────────────────────

_SCHEMA_COLS_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.udt_name,
       c.is_nullable, c.ordinal_position
FROM information_schema.columns c
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position
"""

_SCHEMA_PK_SQL = """
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema = 'public' AND tc.constraint_type = 'PRIMARY KEY'
ORDER BY tc.table_name, kcu.ordinal_position
"""

_SCHEMA_UNIQUE_SQL = """
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
WHERE tc.table_schema = 'public' AND tc.constraint_type = 'UNIQUE'
"""

_SCHEMA_FK_SQL = """
SELECT tc.table_name, kcu.column_name,
       ccu.table_name AS ref_table, ccu.column_name AS ref_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
  ON tc.constraint_name = ccu.constraint_name
 AND tc.table_schema = ccu.table_schema
WHERE tc.table_schema = 'public' AND tc.constraint_type = 'FOREIGN KEY'
"""

_SCHEMA_ENUMS_SQL = """
SELECT t.typname, e.enumlabel
FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
ORDER BY t.typname, e.enumsortorder
"""

_HIDE_TABLES = ("schema_migrations",)


async def dump_schema_text(conn: asyncpg.Connection) -> str:
    """Компактный человеко-читаемый DDL-снимок public-схемы.

    Формат:
        TABLE orders:
          order_id bigint NOT NULL PK
          order_number text NOT NULL UNIQUE
          ...
          order_id -> orders.order_id  (FK строка отдельно)
        ...
        ENUM proc_status: pending, running, done, failed
    """
    cols = await conn.fetch(_SCHEMA_COLS_SQL)
    pk_rows = await conn.fetch(_SCHEMA_PK_SQL)
    uq_rows = await conn.fetch(_SCHEMA_UNIQUE_SQL)
    fk_rows = await conn.fetch(_SCHEMA_FK_SQL)
    enum_rows = await conn.fetch(_SCHEMA_ENUMS_SQL)

    by_table: dict[str, list] = {}
    for c in cols:
        by_table.setdefault(c["table_name"], []).append(c)

    pks: dict[str, set] = {}
    for r in pk_rows:
        pks.setdefault(r["table_name"], set()).add(r["column_name"])

    uqs: dict[str, set] = {}
    for r in uq_rows:
        uqs.setdefault(r["table_name"], set()).add(r["column_name"])

    fks: dict[tuple, tuple] = {}
    for r in fk_rows:
        fks[(r["table_name"], r["column_name"])] = (r["ref_table"], r["ref_column"])

    enum_by: dict[str, list[str]] = {}
    for r in enum_rows:
        enum_by.setdefault(r["typname"], []).append(r["enumlabel"])

    lines: list[str] = []
    for table in sorted(by_table.keys()):
        if table in _HIDE_TABLES:
            continue
        lines.append(f"TABLE {table}:")
        pk_set = pks.get(table, set())
        uq_set = uqs.get(table, set())
        for c in by_table[table]:
            col = c["column_name"]
            t = c["data_type"]
            if t == "USER-DEFINED":
                t = c["udt_name"]
            elif t == "ARRAY":
                t = "array"
            elif t in ("bigint",) and c["udt_name"] == "int8":
                t = "bigint"
            nn = "" if c["is_nullable"] == "YES" else " NOT NULL"
            tags = []
            if col in pk_set:
                tags.append("PK")
            if col in uq_set:
                tags.append("UNIQUE")
            if (table, col) in fks:
                rt, rc = fks[(table, col)]
                tags.append(f"FK->{rt}.{rc}")
            tag = (" " + " ".join(tags)) if tags else ""
            lines.append(f"  {col} {t}{nn}{tag}")
        lines.append("")

    if enum_by:
        for typ in sorted(enum_by):
            lines.append(f"ENUM {typ}: {', '.join(enum_by[typ])}")

    return "\n".join(lines).rstrip() + "\n"
