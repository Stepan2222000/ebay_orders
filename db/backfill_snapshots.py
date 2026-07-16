"""Разовый бэкофилл снапшотов текстов по всей истории листингов.

Этап 2 плана article_truth (article_truth/PLAN.md): ставит снапшоты всем items
без done/failed-снапшота, затем — отложенная сверка титулов. Идемпотентен:
done/failed пропускаются, повторный запуск ничего не ломает. Мёртвые страницы
честно failed — тексты им догружаются руками через API (никаких фолбеков).

Запуск из корня проекта: `python db/backfill_snapshots.py`.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import close, pool
from app.snapshots import ensure_snapshots, reconcile_titles


async def main() -> None:
    logging_fmt = "%(asctime)s %(levelname)s %(message)s"
    import logging
    logging.basicConfig(level=logging.INFO, format=logging_fmt)

    p = await pool()
    nums = [
        r["item_number"]
        for r in await p.fetch(
            """SELECT i.item_number
                 FROM items i
                 LEFT JOIN item_snapshots s USING (item_number)
                WHERE s.item_number IS NULL OR s.status = 'pending'
                ORDER BY i.item_number"""
        )
    ]
    print(f"к снапшоту: {len(nums)} листингов")
    await ensure_snapshots(nums)
    await reconcile_titles()

    print("\n=== статусы ===")
    for r in await p.fetch(
        "SELECT status, source, count(*) c FROM item_snapshots GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        print(f"  {r['status']:8} {r['source'] or '-':7} {r['c']}")
    print("с catalog_url (делистнуты в каталог):",
          await p.fetchval("SELECT count(*) FROM item_snapshots WHERE catalog_url IS NOT NULL"))
    print("титулы сверены:",
          await p.fetchval("SELECT count(*) FROM item_snapshots WHERE title_checked_at IS NOT NULL"))
    print("карточек title_mismatch (open):",
          await p.fetchval("SELECT count(*) FROM review_cards "
                           "WHERE kind = 'title_mismatch' AND status = 'open'"))
    await close()


if __name__ == "__main__":
    asyncio.run(main())
