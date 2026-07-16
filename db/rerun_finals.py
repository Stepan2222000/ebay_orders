"""Разовый принудительный перепрогон ФИНАЛЬНЫХ листингов (agent-строки) на
текущей версии промпта. Нефинальные не трогаем — их сам подхватит воркер по
смене отпечатка. Human-строки не трогаем никогда (авто-guard в apply_truth).

Перед прогоном снимает состояние «до» (статус + состав), после — печатает дифф:
изменённые статусы, изменённые составы (по part_id×qty), смены кросс-номера.
Запуск из корня проекта: `python db/rerun_finals.py`.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import close, pool  # noqa: E402
from app.truth import run_listing  # noqa: E402

CONCURRENCY = 3
SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rerun_finals_before.json")


async def snapshot(p) -> dict:
    rows = await p.fetch("""
        SELECT i.item_number, i.match_status,
               coalesce(json_agg(json_build_object('part_id', ip.part_id,
                        'qty', ip.quantity, 'article', ip.matched_article)
                        ORDER BY ip.part_id) FILTER (WHERE ip.part_id IS NOT NULL),
                        '[]') AS parts
          FROM items i LEFT JOIN item_parts ip USING (item_number)
         GROUP BY i.item_number, i.match_status""")
    return {r["item_number"]: {"status": r["match_status"],
                               "parts": json.loads(r["parts"]) if isinstance(r["parts"], str) else r["parts"]}
            for r in rows}


async def main() -> None:
    import logging
    logging.basicConfig(level=logging.WARNING)
    p = await pool()

    before = await snapshot(p)
    json.dump(before, open(SNAP, "w"), ensure_ascii=False)

    finals = [r["item_number"] for r in await p.fetch("""
        SELECT DISTINCT item_number FROM item_parts
         WHERE match_method = 'agent'
         ORDER BY item_number""")]
    print(f"финальных (agent) к перепрогону: {len(finals)}", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def one(num: str) -> None:
        nonlocal done
        async with sem:
            res = await run_listing(num, write=True)
            done += 1
            v = res["verdict"] if res else "?"
            print(f"[{done}/{len(finals)}] {num}: {v}", flush=True)

    await asyncio.gather(*(one(n) for n in finals))

    after = await snapshot(p)
    print("\n=== ДИФФ ===")
    changed_status, changed_parts, changed_article = [], [], []
    for num in finals:
        b, a = before.get(num, {}), after.get(num, {})
        if b.get("status") != a.get("status"):
            changed_status.append(f"{num}: {b.get('status')} -> {a.get('status')}")
        bp = {(x["part_id"], x["qty"]) for x in b.get("parts", [])}
        ap = {(x["part_id"], x["qty"]) for x in a.get("parts", [])}
        if bp != ap:
            changed_parts.append(f"{num}: {sorted(bp)} -> {sorted(ap)}")
        else:
            ba = {x["part_id"]: x["article"] for x in b.get("parts", [])}
            aa = {x["part_id"]: x["article"] for x in a.get("parts", [])}
            diffs = {k: (ba[k], aa[k]) for k in ba if k in aa and ba[k] != aa[k]}
            for k, (x, y) in diffs.items():
                changed_article.append(f"{num}: {k}: {x} -> {y}")
    print(f"статусы изменились: {len(changed_status)}")
    for s in changed_status:
        print("  ", s)
    print(f"составы (part×qty) изменились: {len(changed_parts)}")
    for s in changed_parts:
        print("  ", s)
    print(f"смена кросс-номера (деталь та же): {len(changed_article)}")
    for s in changed_article:
        print("  ", s)
    await close()


if __name__ == "__main__":
    asyncio.run(main())
