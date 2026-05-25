"""Разовый бэкофилл / пере-матчинг привязок item -> деталь.

Матчит все листинги `items` в статусе 'pending' (после применения 006, после
пополнения каталога или правки правил в brands_mapping). Перечитывает правила из
БД. Требует настроенного FDW (db/setup_fdw.py).

Запуск из корня проекта: `python db/backfill_matches.py`.
"""
import asyncio
import os
import sys
from collections import Counter

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.matching import get_rules, match_listing


async def main() -> None:
    conn = await asyncpg.connect(
        host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
        password=settings.pg_password, database=settings.pg_database,
    )
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL app.source = 'screenshot'")
            rules = await get_rules(conn, refresh=True)
            pending = await conn.fetch(
                "SELECT item_number, item_title FROM items WHERE match_status = 'pending'"
            )
            stats: Counter[str] = Counter()
            for row in pending:
                res = await match_listing(conn, row["item_number"], row["item_title"], rules)
                key = res.status if res.status != "needs_review" else (
                    "needs_review(bundle)" if res.parts else "needs_review(no-candidate)"
                )
                stats[key] += 1
        print(f"matched {len(pending)} pending listings:")
        for k, v in sorted(stats.items()):
            print(f"  {k}: {v}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
