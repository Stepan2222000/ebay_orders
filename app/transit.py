"""Едущие экземпляры в parts_uchet — этап 5 (article_truth/SPEC.md §10).

Принцип freeze: пара (заказ, листинг) с финальной истиной создаётся РОВНО ОДИН
РАЗ — состав запечатлевается на момент создания, дальнейшие смены истины/
листинга на созданных не влияют. Маркер «пара обработана» — журнал
ebay_transit_created в САМОЙ uchet-базе: строки экземпляров и отметка журнала
пишутся одной транзакцией (двух баз — двух транзакций — нет по построению).
Удалённые руками едущие не пересоздаются (журнал остаётся).

Запись — прямым подключением к uchet (FDW-запись валится на audit-триггерах
с неквалифицированными типами, SPEC §14). Никаких карточек по возвратам:
refund — информация, все правки едущих — человек руками (решение 18.07).
"""
import asyncio
import json
import logging
from datetime import date

import asyncpg

from .config import settings
from .db import pool

log = logging.getLogger("transit")

_uchet_pool: asyncpg.Pool | None = None


async def _init_uchet_conn(conn: asyncpg.Connection) -> None:
    for tname in ("jsonb", "json"):
        await conn.set_type_codec(tname, encoder=json.dumps, decoder=json.loads,
                                  schema="pg_catalog")


async def uchet_pool() -> asyncpg.Pool:
    global _uchet_pool
    if _uchet_pool is None:
        _uchet_pool = await asyncpg.create_pool(
            dsn=settings.uchet_pg_dsn, min_size=1, max_size=5,
            max_inactive_connection_lifetime=60, init=_init_uchet_conn)
    return _uchet_pool


# Кандидаты: заказ появился в базе после запуска этапа 5, не отменён (ручная
# пометка / cancel-текст; полный refund признаком НЕ является), истина листинга
# финальна (строки item_parts есть и все — agent/human).
_CANDIDATES_SQL = """
SELECT o.order_id, oi.item_number, oi.item_quantity, o.order_number
  FROM orders o JOIN order_items oi USING (order_id)
 WHERE o.created_at >= $1::date
   AND o.cancelled_at IS NULL
   AND COALESCE(o.delivery_status, '') NOT ILIKE '%cancel%'
   AND EXISTS (SELECT 1 FROM item_parts ip
                WHERE ip.item_number = oi.item_number)
   AND NOT EXISTS (SELECT 1 FROM item_parts ip
                    WHERE ip.item_number = oi.item_number
                      AND ip.match_method NOT IN ('agent', 'human'))
 ORDER BY o.order_id, oi.item_number
"""


async def _create_pair(uconn: asyncpg.Connection, *, order_id: int,
                       order_number: str, item_number: str,
                       item_quantity: int, composition: list[dict],
                       part_names: dict[str, str],
                       condition_note: str | None) -> int:
    """Строки экземпляров + журнал одной транзакцией. 1 строка = 1 штука.
    Возвращает число созданных (0 — пара уже в журнале)."""
    async with uconn.transaction():
        claimed = await uconn.fetchrow(
            """INSERT INTO ebay_transit_created
                   (source_order_id, source_item_number, items_created, composition)
               VALUES ($1, $2, 0, $3)
               ON CONFLICT DO NOTHING RETURNING source_order_id""",
            order_id, item_number, composition)
        if claimed is None:            # freeze: пара уже создавалась
            return 0
        manual = await uconn.fetchval(
            "SELECT count(*) FROM items WHERE source_order_id = $1 "
            "AND source_item_number = $2", order_id, item_number)
        if manual:
            # по паре уже есть экземпляры, заведённые руками (приёмка успела
            # раньше финальной истины) — с человеком не спорим: freeze без
            # создания, журнал фиксирует факт
            log.info("transit: заказ %s × лот %s — %d ручных экземпляров, "
                     "авто-создание пропущено (freeze)", order_number,
                     item_number, manual)
            return 0
        created = 0
        for c in composition:
            n = item_quantity * max(1, c["qty"])
            note = (f"eBay {order_number} · лот {item_number}"
                    f" · {part_names.get(c['part_id'], c['article'])}")
            for _ in range(n):
                await uconn.execute(
                    """INSERT INTO items (smart_part_id, is_in_transit, draft,
                                          source_order_id, source_item_number,
                                          transit_note, condition_note)
                       VALUES ($1, true, true, $2, $3, $4, $5)""",
                    c["part_id"], order_id, item_number, note, condition_note)
                created += 1
        await uconn.execute(
            """UPDATE ebay_transit_created SET items_created = $3
                WHERE source_order_id = $1 AND source_item_number = $2""",
            order_id, item_number, created)
        return created


async def transit_tick() -> int:
    """Один проход создания. Возвращает число созданных экземпляров."""
    ep = await pool()
    up = await uchet_pool()
    async with ep.acquire() as conn:
        cands = await conn.fetch(_CANDIDATES_SQL,
                                 date.fromisoformat(settings.transit_since))
        if not cands:
            return 0
        items = {r["item_number"] for r in cands}
        comp_rows = await conn.fetch(
            """SELECT ip.item_number, ip.part_id, ip.quantity AS qty,
                      ip.matched_article AS article, p.name
                 FROM item_parts ip
                 LEFT JOIN smart_fdw.parts p ON p.id = ip.part_id
                WHERE ip.item_number = ANY($1::text[])
                ORDER BY ip.part_id""", list(items))
        snap_cond = {r["item_number"]: r["condition"] for r in await conn.fetch(
            "SELECT item_number, condition FROM item_snapshots "
            "WHERE item_number = ANY($1::text[])", list(items))}
    comp: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for r in comp_rows:
        comp.setdefault(r["item_number"], []).append(
            {"part_id": r["part_id"], "qty": r["qty"], "article": r["article"]})
        if r["name"]:
            names[r["part_id"]] = r["name"]

    total = 0
    async with up.acquire() as uconn:
        for r in cands:
            cond = snap_cond.get(r["item_number"])
            cond_note = (f"по снапшоту листинга condition={cond}"
                         if cond and "new" not in cond.lower() else None)
            n = await _create_pair(
                uconn, order_id=r["order_id"], order_number=r["order_number"],
                item_number=r["item_number"], item_quantity=r["item_quantity"],
                composition=comp[r["item_number"]], part_names=names,
                condition_note=cond_note)
            if n:
                log.info("transit: заказ %s × лот %s → создано %d едущих",
                         r["order_number"], r["item_number"], n)
                total += n
    return total


async def transit_loop() -> None:
    """Петля создания (третья петля truth_worker)."""
    log.info("transit loop start; since=%s uchet=%s",
             settings.transit_since, settings.uchet_pg_dsn.split("@")[-1])
    while True:
        try:
            await transit_tick()
        except Exception as e:            # noqa: BLE001 — цикл не должен умирать
            log.warning("transit loop: %s: %s", type(e).__name__, e)
        await asyncio.sleep(settings.transit_poll_s)


# ─── Просмотр и ручная правка (API карточки листинга) ────────────────────────

async def transit_info(item_number: str, order_ids: list[int]) -> dict[int, dict]:
    """Сводка едущих по заказам листинга: {order_id: {parts: [...], journal: bool}}."""
    if not order_ids:
        return {}
    up = await uchet_pool()
    rows = await up.fetch(
        """SELECT source_order_id AS order_id, smart_part_id,
                  count(*) FILTER (WHERE draft)::int      AS draft_n,
                  count(*) FILTER (WHERE NOT draft)::int  AS accepted_n
             FROM items
            WHERE source_item_number = $1 AND source_order_id = ANY($2::bigint[])
            GROUP BY 1, 2 ORDER BY 1, 2""", item_number, order_ids)
    journal = {r["source_order_id"] for r in await up.fetch(
        """SELECT source_order_id FROM ebay_transit_created
            WHERE source_item_number = $1 AND source_order_id = ANY($2::bigint[])""",
        item_number, order_ids)}
    out: dict[int, dict] = {}
    for oid in order_ids:
        out[oid] = {"journal": oid in journal, "parts": []}
    for r in rows:
        out[r["order_id"]]["parts"].append(
            {"part_id": r["smart_part_id"], "draft": r["draft_n"],
             "accepted": r["accepted_n"]})
    return out


async def transit_reduce(order_id: int, item_number: str, part_id: str,
                         remove: int) -> int:
    """Удалить до N draft-едущих детали («приедет меньше/не приедет»).
    Принятые защищает штатный guard uchet. Журнал не трогаем — freeze."""
    up = await uchet_pool()
    deleted = await up.fetchval(
        """WITH del AS (
               DELETE FROM items WHERE id IN (
                   SELECT id FROM items
                    WHERE source_order_id = $1 AND source_item_number = $2
                      AND smart_part_id = $3 AND draft AND is_in_transit
                    ORDER BY id DESC LIMIT $4)
               RETURNING id)
           SELECT count(*) FROM del""", order_id, item_number, part_id, remove)
    log.info("transit: заказ %d × лот %s: удалено %d draft-едущих %s",
             order_id, item_number, deleted, part_id)
    return deleted
