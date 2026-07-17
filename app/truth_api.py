"""API истины по артикулам — под разделы UI «Истина» (article_truth/SPEC.md §9).

Разделы: очередь разбора (событийные карточки + вычисляемые статусные списки),
карточка листинга (все данные + действия), «непокрытые номера» (near_articles
+ срезанные гейтом, трёхклассовая разметка по каталогу), редактор правил
brands_mapping с обязательным двухчастным dry-run (кандидаты по текстам +
эффект на гейт по прочитанному агентом) и аудитом.

Ручная правка состава = метод 'human': строки вводятся артикулами, каждая
обязана пройти гейт правил и лукап каталога (то же всё-или-ничего, что у
агента); human-строки замещают всё и агентом автоматически не перетираются.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request

from .db import pool
from .matching import extract_candidates, get_rules
from .truth import redecide_listing, run_listing

log = logging.getLogger(__name__)

truth_api = APIRouter(prefix="/api/truth")

_rerunning: set[str] = set()          # защита от параллельного ручного перепрогона


# ─── Очередь разбора ─────────────────────────────────────────────────────────

_LATEST_RUN_LATERAL = """
LEFT JOIN LATERAL (
    SELECT ar.verdict, ar.positions, ar.contradictions, ar.qty_note,
           ar.raw_response->>'comment' AS comment, ar.created_at
      FROM agent_runs ar
     WHERE ar.item_number = i.item_number AND ar.status = 'done' AND ar.dry_run = false
     ORDER BY ar.created_at DESC LIMIT 1
) run ON true
"""


def _photo_url(item_number: str, photo_id: int | None) -> str | None:
    return f"/api/listings/{item_number}/photos/{photo_id}/image" if photo_id else None


async def _first_photo_ids(conn, nums: list[str]) -> dict[str, int]:
    if not nums:
        return {}
    rows = await conn.fetch(
        """SELECT DISTINCT ON (item_number) item_number, id
             FROM item_photos WHERE item_number = ANY($1::text[])
            ORDER BY item_number, (source = 'manual'), idx""", nums)
    return {r["item_number"]: r["id"] for r in rows}


@truth_api.get("/queue")
async def queue():
    p = await pool()
    async with p.acquire() as conn:
        conflicts = [dict(r) for r in await conn.fetch(f"""
            SELECT i.item_number, i.item_title, i.match_status, i.match_note,
                   run.contradictions, run.qty_note, run.positions,
                   rc.id AS card_id, rc.payload AS card_payload,
                   extract(epoch FROM now() - i.matched_at)::bigint AS age_s
              FROM items i
              LEFT JOIN review_cards rc ON rc.item_number = i.item_number
                   AND rc.status = 'open' AND rc.kind IN ('contradiction', 'human_disagreement')
              {_LATEST_RUN_LATERAL}
             WHERE i.match_status = 'conflict' OR rc.id IS NOT NULL
             ORDER BY i.matched_at DESC NULLS LAST""")]
        nic = [dict(r) for r in await conn.fetch(f"""
            SELECT i.item_number, i.item_title, i.match_status, i.match_note,
                   run.positions,
                   extract(epoch FROM now() - i.matched_at)::bigint AS age_s
              FROM items i {_LATEST_RUN_LATERAL}
             WHERE i.match_status IN ('not_in_catalog', 'no_article')
             ORDER BY i.matched_at DESC NULLS LAST""")]
        need_texts = [dict(r) for r in await conn.fetch("""
            SELECT i.item_number, i.item_title, i.match_status, s.last_error,
                   s.catalog_url,
                   extract(epoch FROM now() - s.updated_at)::bigint AS age_s
              FROM items i JOIN item_snapshots s USING (item_number)
             WHERE s.status = 'failed'
               AND i.match_status IN ('conflict', 'not_in_catalog', 'no_article')
             ORDER BY i.item_number""")]
        title_cards = [dict(r) for r in await conn.fetch("""
            SELECT rc.id AS card_id, rc.item_number, rc.payload, i.item_title,
                   extract(epoch FROM now() - rc.created_at)::bigint AS age_s
              FROM review_cards rc JOIN items i USING (item_number)
             WHERE rc.kind = 'title_mismatch' AND rc.status = 'open'
             ORDER BY rc.created_at""")]
        refunds = [dict(r) for r in await conn.fetch("""
            SELECT rc.id AS card_id, rc.order_id, rc.payload, o.order_number,
                   extract(epoch FROM now() - rc.created_at)::bigint AS age_s
              FROM review_cards rc JOIN orders o ON o.order_id = rc.order_id
             WHERE rc.kind IN ('refund', 'truth_change') AND rc.status = 'open'
             ORDER BY rc.created_at""")]
        photos = await _first_photo_ids(
            conn, [r["item_number"] for r in conflicts + nic + need_texts + title_cards])

    def missing_of(row: dict) -> list[str]:
        return [p.get("canonical") or p.get("article_read", "")
                for p in (row.get("positions") or []) if not p.get("part_id")]

    for r in conflicts + nic + need_texts + title_cards:
        r["photo"] = _photo_url(r["item_number"], photos.get(r["item_number"]))
    for r in nic:
        r["missing"] = missing_of(r)
    for r in conflicts + nic:
        r.pop("positions", None)

    return {
        "counts": {"conflicts": len(conflicts), "not_in_catalog": len(nic),
                   "need_texts": len(need_texts), "title_cards": len(title_cards),
                   "refunds": len(refunds),
                   "total": len(conflicts) + len(nic) + len(need_texts)
                            + len(title_cards) + len(refunds)},
        "linked": await (await pool()).fetchval(
            "SELECT count(*) FROM items WHERE match_status = 'linked'"),
        "total_items": await (await pool()).fetchval("SELECT count(*) FROM items"),
        "conflicts": conflicts, "not_in_catalog": nic, "need_texts": need_texts,
        "title_cards": title_cards, "refunds": refunds,
    }


@truth_api.get("/listings")
async def listings_all():
    """Все листинги (включая успешные) — для режима «Все» в разборе."""
    p = await pool()
    async with p.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch("""
            SELECT i.item_number, i.item_title, i.match_status, i.match_note,
                   ip.composition, ip.methods,
                   extract(epoch FROM now() - i.matched_at)::bigint AS age_s
              FROM items i
              LEFT JOIN LATERAL (
                  SELECT string_agg(p2.matched_article
                                    || CASE WHEN p2.quantity > 1
                                            THEN '×' || p2.quantity ELSE '' END,
                                    ' + ' ORDER BY p2.part_id)  AS composition,
                         array_agg(DISTINCT p2.match_method)     AS methods
                    FROM item_parts p2 WHERE p2.item_number = i.item_number
              ) ip ON true
             ORDER BY i.matched_at DESC NULLS LAST""")]
        photos = await _first_photo_ids(conn, [r["item_number"] for r in rows])
    for r in rows:
        r["photo"] = _photo_url(r["item_number"], photos.get(r["item_number"]))
    return {"listings": rows}


@truth_api.get("/badge")
async def badge():
    p = await pool()
    n = await p.fetchval("""
        SELECT (SELECT count(*) FROM review_cards WHERE status = 'open')
             + (SELECT count(*) FROM items
                 WHERE match_status IN ('not_in_catalog', 'no_article')
                   AND NOT EXISTS (SELECT 1 FROM review_cards rc
                                    WHERE rc.item_number = items.item_number
                                      AND rc.status = 'open'))""")
    return {"open": n}


# ─── Карточка листинга ───────────────────────────────────────────────────────

@truth_api.get("/listing/{item_number}")
async def listing(item_number: str):
    p = await pool()
    async with p.acquire() as conn:
        item = await conn.fetchrow(
            "SELECT item_number, item_title, match_status, match_note, matched_at "
            "FROM items WHERE item_number = $1", item_number)
        if item is None:
            raise HTTPException(404, "листинг не найден")
        snap = await conn.fetchrow(
            "SELECT * FROM item_snapshots WHERE item_number = $1", item_number)
        photos = [
            {"id": r["id"], "source": r["source"],
             "url": _photo_url(item_number, r["id"])}
            for r in await conn.fetch(
                "SELECT id, source FROM item_photos WHERE item_number = $1 "
                "ORDER BY (source = 'manual'), idx, id", item_number)]
        comp = [dict(r) for r in await conn.fetch("""
            SELECT ip.part_id, ip.matched_article, ip.quantity, ip.match_method,
                   p.name AS part_name
              FROM item_parts ip
              LEFT JOIN smart_fdw.parts p ON p.id = ip.part_id
             WHERE ip.item_number = $1 ORDER BY ip.part_id""", item_number)]
        orders = [dict(r) for r in await conn.fetch("""
            SELECT o.order_id, o.order_number, oi.item_quantity, o.delivery_status,
                   o.delivered_date
              FROM order_items oi JOIN orders o USING (order_id)
             WHERE oi.item_number = $1 ORDER BY o.order_id""", item_number)]
        runs = [dict(r) for r in await conn.fetch("""
            SELECT id, status, dry_run, verdict, positions, near_articles,
                   contradictions, qty_note, raw_response->>'comment' AS comment,
                   raw_response->>'lot_kind' AS lot_kind, model, error,
                   prompt_tokens, completion_tokens, created_at, finished_at,
                   (input_context ? 'redecided_from_run') AS no_llm
              FROM agent_runs WHERE item_number = $1
             ORDER BY created_at DESC LIMIT 5""", item_number)]
        cards = [dict(r) for r in await conn.fetch("""
            SELECT id, kind, payload, status, resolution, created_at, resolved_at
              FROM review_cards WHERE item_number = $1
             ORDER BY created_at DESC""", item_number)]
        running = any(r["status"] in ("queued", "running") for r in runs) \
            or item_number in _rerunning
    return {"item": dict(item), "snapshot": dict(snap) if snap else None,
            "photos": photos, "composition": comp, "orders": orders,
            "runs": runs, "cards": cards, "agent_running": running}


@truth_api.post("/listing/{item_number}/rerun")
async def rerun(item_number: str):
    """Ручной перепрогон (в т.ч. финальных — SPEC §6 «Перепрогоны»)."""
    p = await pool()
    if item_number in _rerunning:
        raise HTTPException(409, "прогон уже идёт")
    exists = await p.fetchval(
        "SELECT EXISTS(SELECT 1 FROM items WHERE item_number = $1)", item_number)
    if not exists:
        raise HTTPException(404, "листинг не найден")

    async def _bg():
        try:
            await run_listing(item_number, write=True)
        except Exception as e:            # noqa: BLE001
            log.warning("manual rerun %s: %s: %s", item_number, type(e).__name__, e)
        finally:
            _rerunning.discard(item_number)

    _rerunning.add(item_number)
    asyncio.create_task(_bg())
    return {"started": True}


@truth_api.post("/listing/{item_number}/recheck-catalog")
async def recheck_catalog(item_number: str):
    """Перерешив без LLM (SPEC §6): «завёл деталь в smart → проверил» —
    мгновенно и бесплатно. Полный прогон остаётся для изменений чтения-входа."""
    if item_number in _rerunning:
        raise HTTPException(409, "идёт полный прогон агента")
    p = await pool()
    final = await p.fetchval(
        "SELECT EXISTS(SELECT 1 FROM item_parts WHERE item_number = $1 "
        "AND match_method IN ('agent', 'human'))", item_number)
    if final:
        raise HTTPException(409, "истина финальна — перерешив не нужен")
    return await redecide_listing(item_number, write=True)


# ─── Ручной состав (human) ───────────────────────────────────────────────────

async def _resolve_lines(conn, lines: list[dict]) -> list[dict]:
    """[{article, qty}] → резолв через гейт правил и каталог (без записи)."""
    rules = await get_rules(conn, refresh=True)  # правила правятся напрямую в БД
    out = []
    for ln in lines:
        raw = str(ln.get("article", "")).strip()
        qty = max(1, int(ln.get("qty", 1) or 1))
        cands = extract_candidates(raw, rules) if raw else set()
        up = raw.upper()
        tries = [up, up.replace(" ", "")] + sorted(cands, key=len, reverse=True)
        best, part = None, None
        for c in dict.fromkeys(t for t in tries if t):
            row = await conn.fetchrow(
                "SELECT pa.article, pa.part_id, p.name FROM smart_fdw.part_articles pa "
                "JOIN smart_fdw.parts p ON p.id = pa.part_id "
                "WHERE upper(pa.article) = $1", c)
            if row:
                best, part = row["article"], dict(row)
                break
        if best is None and cands:
            best = max(cands, key=len)
        out.append({
            "article": raw, "qty": qty, "canonical": best,
            "part_id": part["part_id"] if part else None,
            "part_name": part["name"] if part else None,
            "gate": "ok" if (cands or part) else "uncovered",
        })
    return out


@truth_api.post("/resolve")
async def resolve_preview(request: Request):
    """Живая валидация строк состава при вводе (ничего не пишет)."""
    body = await request.json()
    p = await pool()
    async with p.acquire() as conn:
        return {"lines": await _resolve_lines(conn, body.get("lines") or [])}


def _norm_num(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).strip().upper()


async def _classify_example(conn, resolved: list[dict], run) -> str:
    """Детерминированная причина расхождения (не LLM): смотрим на ПРАВИЛЬНЫЙ
    (человеческий) номер. Не проходит правила → «rule» (дыра в brands_mapping,
    даже если внешне ошибка смысловая — без правила агент был слеп). Проходит,
    но в прогоне его не было ни в подсказках, ни в прочитанном → «not_seen».
    Был полностью доступен → «semantic»."""
    rules = await get_rules(conn, refresh=True)  # правила правятся напрямую в БД
    ctx = (run["input_context"] or {}) if run else {}
    hints = {_norm_num(c) for c in (ctx.get("candidates") or {})}
    seen: set[str] = set()
    for pp in (run["positions"] or []) if run else []:
        seen |= {_norm_num(pp.get("article_read", "")), _norm_num(pp.get("canonical") or "")}
    for na in (run["near_articles"] or []) if run else []:
        seen.add(_norm_num(na.get("text", "")))
    seen.discard("")

    kinds: set[str] = set()
    for r in resolved:
        n_raw, n_canon = _norm_num(r["article"]), _norm_num(r.get("canonical") or "")
        covered = bool(extract_candidates(r["article"], rules))
        if not covered:
            kinds.add("rule")
        elif run is None:
            kinds.add("unclear")
        elif not ({n_raw, n_canon} & (hints | seen)):
            kinds.add("not_seen")
        else:
            kinds.add("semantic")
    for k in ("rule", "not_seen", "semantic"):
        if k in kinds:
            return k
    return "unclear"


@truth_api.put("/listing/{item_number}/composition")
async def put_composition(item_number: str, request: Request):
    """Ручная правка состава → human-строки (всё-или-ничего, как у агента).
    Если правка меняет истину относительно агентской — автоматически пишется
    пример с меткой причины (материал для правки правил/промпта)."""
    body = await request.json()
    lines = body.get("lines") or []
    if not lines:
        raise HTTPException(400, "состав пуст")
    p = await pool()
    async with p.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM items WHERE item_number = $1)", item_number)
        if not exists:
            raise HTTPException(404, "листинг не найден")
        resolved = await _resolve_lines(conn, lines)
        bad = [r for r in resolved if not r["part_id"]]
        if bad:
            raise HTTPException(422, detail={"lines": resolved})

        prior = [dict(r) for r in await conn.fetch(
            "SELECT part_id, quantity, match_method FROM item_parts "
            "WHERE item_number = $1", item_number)]
        run = await conn.fetchrow("""
            SELECT id, verdict, positions, near_articles, contradictions,
                   input_context
              FROM agent_runs
             WHERE item_number = $1 AND status = 'done' AND dry_run = false
             ORDER BY created_at DESC LIMIT 1""", item_number)

        example_kind = None
        async with conn.transaction():
            agg: dict[str, dict] = {}
            for r in resolved:
                a = agg.setdefault(r["part_id"], {"article": r["canonical"], "qty": 0})
                a["qty"] += r["qty"]
            await conn.execute(
                "DELETE FROM item_parts WHERE item_number = $1", item_number)
            for part_id, a in agg.items():
                await conn.execute(
                    """INSERT INTO item_parts(item_number, part_id, quantity,
                                              matched_article, match_method)
                       VALUES ($1, $2, $3, $4, 'human')""",
                    item_number, part_id, a["qty"], a["article"])
            note = "human: " + "; ".join(
                f"{a['article']}×{a['qty']}" for a in agg.values())
            await conn.execute(
                "UPDATE items SET match_status = 'linked', matched_at = now(), "
                "match_note = $2 WHERE item_number = $1", item_number, note[:500])
            await conn.execute(
                """UPDATE review_cards
                      SET status = 'resolved', resolved_at = now(),
                          resolution = 'закрыто ручной правкой состава'
                    WHERE item_number = $1 AND status = 'open'
                      AND kind IN ('contradiction', 'human_disagreement')""",
                item_number)

            # Пример — только когда human-состав реально отличается от
            # ПРОЧИТАННОГО агентом. Подтверждение прочитанного как есть
            # (например, закрытие конфликта «согласен с агентом») — штатный
            # разбор, не материал для правки правил/промпта.
            read_agg: dict[str, int] = {}
            for pp in (run["positions"] or []) if run else []:
                if pp.get("part_id"):
                    read_agg[pp["part_id"]] = (read_agg.get(pp["part_id"], 0)
                                               + max(1, pp.get("qty") or 1))
            read_unresolved = bool(run) and any(
                not pp.get("part_id") for pp in (run["positions"] or []))
            human_agg = {pid: a["qty"] for pid, a in agg.items()}
            changed = bool(run) and (human_agg != read_agg or read_unresolved)
            if changed:
                example_kind = await _classify_example(conn, resolved, run)
                await conn.execute(
                    """INSERT INTO match_examples(item_number, kind, human_lines,
                                                  agent_snapshot, run_id, note)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    item_number, example_kind, resolved,
                    {"verdict": run["verdict"], "positions": run["positions"],
                     "near_articles": run["near_articles"],
                     "contradictions": run["contradictions"],
                     "input_context": run["input_context"],
                     "prior_parts": prior} if run else {"prior_parts": prior},
                    run["id"] if run else None,
                    (body.get("note") or "").strip() or None)
    return {"composition": resolved, "example_kind": example_kind}


@truth_api.get("/examples")
async def examples_list():
    p = await pool()
    rows = await p.fetch("""
        SELECT e.id, e.item_number, e.kind, e.human_lines, e.agent_snapshot,
               e.note, e.created_at, i.item_title
          FROM match_examples e JOIN items i USING (item_number)
         ORDER BY e.created_at DESC LIMIT 500""")
    return {"examples": [dict(r) for r in rows]}


@truth_api.delete("/examples/{example_id}")
async def example_delete(example_id: int):
    p = await pool()
    n = await p.execute("DELETE FROM match_examples WHERE id = $1", example_id)
    if n == "DELETE 0":
        raise HTTPException(404, "примера нет")
    return {"deleted": example_id}


# ─── Карточки ────────────────────────────────────────────────────────────────

@truth_api.post("/cards/{card_id}/resolve")
async def resolve_card(card_id: int, request: Request):
    body = await request.json()
    resolution = (body.get("resolution") or "").strip() or "закрыто вручную"
    canon = body.get("title_canon")          # для title_mismatch: 'ocr' | 'pdp'
    p = await pool()
    async with p.acquire() as conn:
        card = await conn.fetchrow(
            "SELECT id, kind, item_number, payload FROM review_cards "
            "WHERE id = $1 AND status = 'open'", card_id)
        if card is None:
            raise HTTPException(404, "открытой карточки нет")
        async with conn.transaction():
            if card["kind"] == "title_mismatch" and canon in ("ocr", "pdp"):
                title = card["payload"].get(f"{canon}_title")
                if title:
                    await conn.execute(
                        "UPDATE items SET item_title = $2 WHERE item_number = $1",
                        card["item_number"], title)
                resolution = f"канон = {canon.upper()}"
            await conn.execute(
                "UPDATE review_cards SET status = 'resolved', resolved_at = now(), "
                "resolution = $2 WHERE id = $1", card_id, resolution)
    return {"resolved": card_id}


# ─── «Замеченные номера» ─────────────────────────────────────────────────────

_TRASH_WHY = re.compile(
    r"штрих|баркод|upc|ean|дата|год[ыау]?|колич|цена|индекс|адрес|парти|серийн|печат|lot"
    r"|внутренн|шумов|код продавца|код позиции|номер позиции|складск",
    re.IGNORECASE)


@truth_api.get("/numbers")
async def numbers():
    """Номера группируются по ЦЕЛЬНОМУ тексту, как прочитал агент (разрезание
    правилами давало осколки и ложные привязки к листингам). Классы — по
    каталогу; явный мусор (агент сам пишет «штрихкод/дата») прячется."""
    p = await pool()
    async with p.acquire() as conn:
        rules = await get_rules(conn, refresh=True)  # правила правятся напрямую в БД
        rows = await conn.fetch("""
            SELECT DISTINCT ON (item_number) item_number, positions, near_articles
              FROM agent_runs WHERE status = 'done' AND dry_run = false
             ORDER BY item_number, created_at DESC""")
        ignores = {r["normalized"]: r["reason"] for r in await conn.fetch(
            "SELECT normalized, reason FROM near_article_ignores")}

        entries: dict[str, dict] = {}     # цельный номер -> агрегат
        part_arts: dict[str, set[str]] = {}
        for r in rows:
            raw = [(n["text"], n.get("why", "")) for n in (r["near_articles"] or [])]
            raw += [(pp["article_read"], "не прошёл гейт правил")
                    for pp in (r["positions"] or []) if pp.get("gate") == "uncovered"]
            pids = [pp["part_id"] for pp in (r["positions"] or []) if pp.get("part_id")]
            for pid in pids:
                part_arts.setdefault(pid, set())
            for text, why in raw:
                key = re.sub(r"\s+", " ", (text or "")).strip().upper()
                if len(key.replace(" ", "")) < 4:
                    continue
                e = entries.setdefault(key, {"normalized": key, "count": 0,
                                             "listings": set(), "why": why[:120],
                                             "own_parts": set(), "trash": True})
                e["count"] += 1
                e["listings"].add(r["item_number"])
                e["own_parts"].update(pids)
                if not _TRASH_WHY.search(why or ""):
                    e["trash"] = False    # хоть одно осмысленное упоминание — не мусор

        if part_arts:
            for r in await conn.fetch(
                    "SELECT id, articles FROM smart_fdw.parts WHERE id = ANY($1::text[])",
                    list(part_arts)):
                part_arts[r["id"]] = {a.upper() for a in (r["articles"] or [])}

        lookup_map: dict[str, set[str]] = {}
        for key in entries:
            variants = {key, key.replace(" ", "")} | extract_candidates(key, rules)
            lookup_map[key] = {v for v in variants if v}
        all_variants = sorted({v for vs in lookup_map.values() for v in vs})
        cat = {r["art"]: r["part_id"] for r in await conn.fetch(
            "SELECT upper(article) AS art, part_id FROM smart_fdw.part_articles "
            "WHERE upper(article) = ANY($1::text[])", all_variants)} if all_variants else {}

    candidates, conflicts, ignored, known, trash = [], [], [], 0, 0
    for key, e in entries.items():
        own = set().union(*(part_arts.get(pid, set()) for pid in e["own_parts"])) \
            if e["own_parts"] else set()
        hits = {v: cat[v] for v in lookup_map[key] if v in cat}
        row = {"normalized": key, "count": e["count"],
               "listings": sorted(e["listings"]), "why": e["why"]}
        if key in ignores:
            ignored.append({**row, "reason": ignores[key]})
        elif lookup_map[key] & own:
            known += 1                     # уже кросс сматченной детали — прячем
        elif hits:
            row["other_part_id"] = next(iter(hits.values()))
            conflicts.append(row)
        elif e["trash"]:
            trash += 1                     # агент сам назвал мусором — прячем
        else:
            candidates.append(row)
    candidates.sort(key=lambda r: -r["count"])
    conflicts.sort(key=lambda r: -r["count"])
    ignored.sort(key=lambda r: r["normalized"])
    return {"candidates": candidates, "catalog_conflicts": conflicts,
            "ignored": ignored, "hidden_known_crosses": known, "hidden_trash": trash}


@truth_api.post("/numbers/ignore")
async def ignore_number(request: Request):
    body = await request.json()
    normalized = (body.get("normalized") or "").strip().upper()
    if not normalized:
        raise HTTPException(400, "пустой номер")
    p = await pool()
    await p.execute(
        "INSERT INTO near_article_ignores(normalized, reason) VALUES ($1, $2) "
        "ON CONFLICT (normalized) DO UPDATE SET reason = EXCLUDED.reason",
        normalized, (body.get("reason") or "").strip() or None)
    return {"ignored": normalized}


@truth_api.delete("/numbers/ignore/{normalized}")
async def unignore_number(normalized: str):
    p = await pool()
    await p.execute("DELETE FROM near_article_ignores WHERE normalized = $1",
                    normalized.upper())
    return {"unignored": normalized.upper()}


# ─── Правила ─────────────────────────────────────────────────────────────────

@truth_api.get("/rules")
async def rules_list():
    p = await pool()
    rows = await p.fetch(
        "SELECT name, canonical, find_regex, note, enabled, example_from, example_to "
        "FROM brands_fdw.article_match_rules ORDER BY name")
    audit = await p.fetch(
        "SELECT rule_name, action, note, created_at FROM brands_fdw.rule_audit "
        "ORDER BY created_at DESC LIMIT 50")
    brands = [r["canonical"] for r in await p.fetch(
        "SELECT canonical FROM brands_fdw.brands ORDER BY canonical")]
    return {"rules": [dict(r) for r in rows], "audit": [dict(r) for r in audit],
            "brands": brands}


async def _dry_run_diff(conn, name: str, find_regex: str, enabled: bool) -> dict:
    """Двухчастный dry-run: (а) дифф кандидатов по всем текстам,
    (б) эффект на гейт по прочитанному агентом. Прогнано прототипами."""
    try:
        new_rx = re.compile(find_regex)
    except re.error as e:
        raise HTTPException(422, f"кривой regex: {e}")
    cur = await conn.fetch(
        "SELECT name, find_regex FROM brands_fdw.article_match_rules WHERE enabled")
    old_rules = [(r["name"], re.compile(r["find_regex"])) for r in cur]
    new_rules = [(n, rx) for n, rx in old_rules if n != name]
    if enabled:
        new_rules.append((name, new_rx))

    texts = await conn.fetch("""
        SELECT i.item_number,
               i.item_title || ' ' || coalesce(s.specifics::text, '') || ' '
               || coalesce(s.specifics_raw, '') || ' ' || coalesce(s.description, '')
               AS txt
          FROM items i LEFT JOIN item_snapshots s USING (item_number)""")
    gained, lost = [], []
    for r in texts:
        o = extract_candidates(r["txt"] or "", old_rules)
        n = extract_candidates(r["txt"] or "", new_rules)
        for c in sorted(n - o):
            gained.append({"item_number": r["item_number"], "candidate": c})
        for c in sorted(o - n):
            lost.append({"item_number": r["item_number"], "candidate": c})
    for coll in (gained, lost):
        cands = list({x["candidate"] for x in coll})
        if cands:
            hits = {r["art"]: r["part_id"] for r in await conn.fetch(
                "SELECT upper(article) art, part_id FROM smart_fdw.part_articles "
                "WHERE upper(article) = ANY($1::text[])", cands)}
            for x in coll:
                x["part_id"] = hits.get(x["candidate"])

    reads = await conn.fetch("""
        SELECT DISTINCT ON (item_number) item_number, positions, near_articles
          FROM agent_runs WHERE status = 'done' AND dry_run = false
         ORDER BY item_number, created_at DESC""")

    def passes(s: str, rules) -> bool:
        up = (s or "").upper()
        return any(rx.search(up) for _, rx in rules)

    gate_pass, gate_fail = [], []
    for r in reads:
        strings = [pp["article_read"] for pp in (r["positions"] or [])]
        strings += [na["text"] for na in (r["near_articles"] or [])]
        for s in set(strings):
            o, n = passes(s, old_rules), passes(s, new_rules)
            if n and not o:
                gate_pass.append({"item_number": r["item_number"], "text": s})
            elif o and not n:
                gate_fail.append({"item_number": r["item_number"], "text": s})

    affected = sorted({x["item_number"] for x in gained + lost + gate_pass + gate_fail})
    nonfinal = {r["item_number"] for r in await conn.fetch("""
        SELECT item_number FROM items i
         WHERE NOT EXISTS (SELECT 1 FROM item_parts ip
                            WHERE ip.item_number = i.item_number
                              AND ip.match_method IN ('agent', 'human'))""")}
    return {"texts_gained": gained[:200], "texts_lost": lost[:200],
            "gate_now_passing": gate_pass[:200], "gate_now_failing": gate_fail[:200],
            "affected_nonfinal": sorted(set(affected) & nonfinal)}


@truth_api.post("/rules/dry-run")
async def rules_dry_run(request: Request):
    body = await request.json()
    p = await pool()
    async with p.acquire() as conn:
        return await _dry_run_diff(conn, (body.get("name") or "").strip(),
                                   body.get("find_regex") or "",
                                   bool(body.get("enabled", True)))


@truth_api.put("/rules/{name}")
async def rules_save(name: str, request: Request):
    """Создать/править правило. Правила общие для проектов — каждая правка в аудит."""
    body = await request.json()
    find_regex = (body.get("find_regex") or "").strip()
    if not find_regex:
        raise HTTPException(400, "пустой find_regex")
    try:
        re.compile(find_regex)
    except re.error as e:
        raise HTTPException(422, f"кривой regex: {e}")
    enabled = bool(body.get("enabled", True))
    p = await pool()
    async with p.acquire() as conn:
        old = await conn.fetchrow(
            "SELECT name, canonical, find_regex, note, enabled "
            "FROM brands_fdw.article_match_rules WHERE name = $1", name)
        async with conn.transaction():
            if old is None:
                canonical = (body.get("canonical") or "").strip().upper()
                brand_ok = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM brands_fdw.brands WHERE canonical = $1)",
                    canonical)
                if not brand_ok:
                    raise HTTPException(
                        422, f"canonical «{canonical}» не из справочника брендов")
                await conn.execute(
                    """INSERT INTO brands_fdw.article_match_rules
                           (name, canonical, find_regex, note, enabled,
                            example_from, example_to)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    name, canonical, find_regex, body.get("note") or "", enabled,
                    body.get("example_from"), body.get("example_to"))
                action = "create"
            else:
                await conn.execute(
                    """UPDATE brands_fdw.article_match_rules
                          SET find_regex = $2, note = coalesce($3, note),
                              enabled = $4
                        WHERE name = $1""",
                    name, find_regex, body.get("note"), enabled)
                action = "update" if old["enabled"] == enabled else (
                    "enable" if enabled else "disable")
            await conn.execute(
                "INSERT INTO brands_fdw.rule_audit(rule_name, action, old_value, "
                "new_value, note) VALUES ($1, $2, $3, $4, $5)",
                name, action, dict(old) if old else None,
                {"find_regex": find_regex, "enabled": enabled,
                 "note": body.get("note")},
                (body.get("audit_note") or "").strip() or None)
        await get_rules(conn, refresh=True)      # кеш этого процесса
    return {"saved": name, "action": action}
