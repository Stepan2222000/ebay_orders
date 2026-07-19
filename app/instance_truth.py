"""Контур 2 — настоящая маркировка экземпляра (article_truth/SPEC.md §12).

Агент читает НАШИ складские фото экземпляра (parts_photos, owner_kind
'instance'); маркировка не видна — фолбэк на фото листинга-источника с
классификацией «реальные фото экземпляра vs сток-картинки» (правило ПОД
ВОПРОСОМ — SPEC §12). Код решает:
- прочитанный номер ∈ articles своей smart-детали → items.primary_article
  (пишется КАТАЛОЖНАЯ форма из articles; БД-триггер uchet валидирует то же);
- номер принадлежит другой smart-детали → is_tentative + tentative_note
  («вручную проверь» — готовый механизм uchet, привязку не меняем никогда);
- не прочитано нигде → no_article; экземпляр не крутится, пока фото не
  изменятся (отпечаток).

Правки человека неприкосновенны: кандидаты — только primary_article IS NULL
и NOT is_tentative; вписал/снял руками — агент не возвращается без новых фото.

Режимы (INSTANCE_TRUTH): off — петля не стартует (дефолт; ворота включения за
пользователем), dry — только журнал instance_runs, write — боевой.
"""
import asyncio
import base64
import hashlib
import json
import logging

import asyncpg
import httpx
from openai import APIError

from .config import settings
from .db import pool
from .matching import extract_candidates, get_rules
from .transit import uchet_pool
from .truth import _http_client, _llm_client

log = logging.getLogger("instance")

PROMPT_REV = "i1"    # версия промпта контура 2; входит в отпечаток
DECISION_REV = "d2"  # версия решателя (тоже в отпечатке: правка решателя →
                     # перечитка; d2 — прочитанное гоняется и через правила
                     # brands_mapping, кейс «57-862087 DF» → 862087)

_photos_pool: asyncpg.Pool | None = None


async def photos_pool() -> asyncpg.Pool:
    global _photos_pool
    if _photos_pool is None:
        _photos_pool = await asyncpg.create_pool(
            dsn=settings.photos_pg_dsn, min_size=1, max_size=3,
            max_inactive_connection_lifetime=60)
    return _photos_pool


SCHEMA = {
    "type": "object",
    "properties": {
        "numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "where": {"type": "string"},
                },
                "required": ["text", "where"],
                "additionalProperties": False,
            },
        },
        "photos_real": {
            "type": "boolean",
            "description": "true if these are photos of the actual physical unit (labels, wear, background); false for stock/catalog renders",
        },
        "note": {"type": "string"},
    },
    "required": ["numbers", "photos_real", "note"],
    "additionalProperties": False,
}

PROMPT = """You read part-number markings on photos of ONE physical marine part for a warehouse.

The part is catalogued as: {part_name}
Known article numbers of this part (any of them may be printed on it): {articles}

TASK: list every part-number-like marking actually visible in the photos (printed label, casting, engraving, sticker). Read character by character; do not guess or complete from the catalog list — output only what is physically legible. Serial numbers, barcodes, dates and batch codes are NOT part numbers — omit them. If no part-number marking is legible, return an empty list.
Also judge: are these photos of the actual physical unit (real item: labels, wear, real background) or stock/catalog renders?
photos: {n_photos}. Write note in Russian."""


def _norm(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


async def _llm_read(photo_urls: list[str], part_name: str,
                    articles: list[str]) -> tuple[dict, dict, str]:
    content: list[dict] = [{"type": "text", "text": PROMPT.format(
        part_name=part_name, articles=", ".join(articles) or "(none)",
        n_photos=len(photo_urls))}]
    for u in photo_urls:
        img = (await _http_client().get(u)).content
        mime = "image/png" if u.endswith(".png") else "image/jpeg"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}})
    resp = await _llm_client().chat.completions.create(
        model=settings.truth_model,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "instance_marking",
                                         "strict": True, "schema": SCHEMA}},
        max_tokens=1500,
    )
    ans = json.loads(resp.choices[0].message.content)
    usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
             "completion_tokens": getattr(resp.usage, "completion_tokens", None)}
    return ans, usage, resp.model or settings.truth_model


async def _gather(inst: dict) -> dict | None:
    """Вход прогона: наши фото, articles детали, фото продавца (если есть
    source-привязка). None — фото экземпляра исчезли (не кандидат)."""
    pp = await photos_pool()
    our = [r["s3_key"] for r in await pp.fetch(
        """SELECT p.s3_key
             FROM photo_collages c JOIN photos p ON p.collage_id = c.id
            WHERE c.owner_kind = 'instance' AND c.owner_id = $1
              AND p.state = 'uploaded'
            ORDER BY p.position""", str(inst["id"]))]
    if not our:
        return None
    ep = await pool()
    part = await ep.fetchrow(
        "SELECT name, articles FROM smart_fdw.parts WHERE id = $1",
        inst["smart_part_id"])
    seller: list[str] = []
    if inst["source_item_number"]:
        seller = [r["s3_url"] for r in await ep.fetch(
            """SELECT s3_url FROM item_photos WHERE item_number = $1
               ORDER BY (source = 'manual'), idx LIMIT $2""",
            inst["source_item_number"], settings.truth_max_photos)]
    inp = {
        "our_urls": [f"{settings.photos_minio_base}/{k}" for k in our],
        "seller_urls": seller,
        "part_name": part["name"],
        "articles": list(part["articles"] or []),
    }
    inp["fingerprint"] = hashlib.sha256(json.dumps(
        {"our": our, "seller": seller, "articles": inp["articles"],
         "model": settings.truth_model, "prompt_rev": PROMPT_REV,
         "decision_rev": DECISION_REV},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return inp


async def _decide(ep_conn, inst: dict, inp: dict, ans: dict,
                  source: str) -> tuple[str | None, dict]:
    """Код решает по прочитанному. → (verdict|None, детали).
    None — источник ничего не дал (можно пробовать следующий).

    Каждый прочитанный текст сверяется и сырым, и через правила brands_mapping
    (переиспользование контура 1: дилерский префикс «57-», пробельные и
    дефисные формы срезаются/склеиваются правилами)."""
    rules = await get_rules(ep_conn)
    own_by_norm = {_norm(a): a for a in inp["articles"]}
    read = [n["text"] for n in ans["numbers"] if n["text"].strip()]

    def forms(r: str) -> list[str]:
        return list(dict.fromkeys(
            [r.strip().upper(), *sorted(extract_candidates(r, rules),
                                        key=len, reverse=True)]))

    for r in read:
        for f in forms(r):
            stored = own_by_norm.get(_norm(f))
            if stored:
                return "primary", {"article": stored, "read": r, "source": source}
    for r in read:
        for f in forms(r):
            row = await ep_conn.fetchrow(
                """SELECT pa.part_id, p.name FROM smart_fdw.part_articles pa
                     JOIN smart_fdw.parts p ON p.id = pa.part_id
                    WHERE upper(pa.article) = $1 AND pa.part_id <> $2""",
                f, inst["smart_part_id"])
            if row:
                return "tentative", {"read": r, "other_part_id": row["part_id"],
                                     "other_name": row["name"], "source": source}
    return None, {"read": read}


async def run_instance(inst: dict, *, write: bool) -> str | None:
    """Полный цикл одного экземпляра. Возвращает вердикт (None — пропуск)."""
    ep = await pool()
    inp = await _gather(inst)
    if inp is None:
        return None
    async with ep.acquire() as conn:
        done = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM instance_runs
                WHERE instance_id = $1 AND input_fingerprint = $2
                  AND status = 'done' AND (dry_run = false OR $3::boolean = false))""",
            inst["id"], inp["fingerprint"], write)
        if done:
            return None
        run_id = await conn.fetchval(
            """INSERT INTO instance_runs (instance_id, input_fingerprint, status,
                                          dry_run, model)
               VALUES ($1, $2, 'running', $3, $4) RETURNING id""",
            inst["id"], inp["fingerprint"], not write, settings.truth_model)

    try:
        ans, usage, model = await _llm_read(
            inp["our_urls"], inp["part_name"], inp["articles"])
        async with ep.acquire() as conn:
            verdict, det = await _decide(conn, inst, inp, ans, "our_photos")
        seller_real = None
        if verdict is None and inp["seller_urls"]:
            ans2, usage2, model = await _llm_read(
                inp["seller_urls"], inp["part_name"], inp["articles"])
            usage = {k: (usage.get(k) or 0) + (usage2.get(k) or 0) for k in usage}
            seller_real = ans2["photos_real"]
            if ans2["photos_real"]:   # сток-картинки для маркировки не годятся
                async with ep.acquire() as conn:
                    verdict, det = await _decide(conn, inst, inp, ans2,
                                                 "seller_photos")
            ans = {"our": ans, "seller": ans2}
        final = verdict or "no_article"
    except (APIError, httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        async with ep.acquire() as conn:
            await conn.execute(
                "UPDATE instance_runs SET status='failed', error=$2, "
                "finished_at=now() WHERE id=$1",
                run_id, f"{type(e).__name__}: {str(e)[:400]}")
        log.warning("instance %s: failed — %s: %s", inst["id"], type(e).__name__, e)
        return "failed"

    if write:
        up = await uchet_pool()
        if final == "primary":
            await up.execute(
                """UPDATE items SET primary_article = $2
                    WHERE id = $1 AND primary_article IS NULL""",
                inst["id"], det["article"])
        elif final == "tentative":
            note = (f"агент: на фото ({'наших' if det['source'] == 'our_photos' else 'листинга'}) "
                    f"маркировка {det['read']} — это деталь "
                    f"«{det['other_name']}» ({det['other_part_id']}), "
                    f"а экземпляр привязан к «{inp['part_name']}» — проверь")
            await up.execute(
                """UPDATE items SET is_tentative = true, tentative_note = $2
                    WHERE id = $1 AND NOT is_tentative""",
                inst["id"], note[:500])

    article_read = det.get("article") or (
        det.get("read") if isinstance(det.get("read"), str) else None)
    async with ep.acquire() as conn:
        await conn.execute(
            """UPDATE instance_runs
                  SET status='done', verdict=$2, article_read=$3, source=$4,
                      seller_photos_real=$5, raw_response=$6, model=$7,
                      prompt_tokens=$8, completion_tokens=$9, finished_at=now()
                WHERE id = $1""",
            run_id, final, article_read,
            det.get("source"), seller_real, ans, model,
            usage["prompt_tokens"], usage["completion_tokens"])
    log.info("instance %s (%s): %s %s (write=%s)", inst["id"],
             inp["part_name"], final, det, write)
    return final


_CANDIDATES_SQL = """
SELECT id, smart_part_id, source_item_number
  FROM items
 WHERE status = 'in_stock' AND primary_article IS NULL AND NOT is_tentative
 ORDER BY id
"""


async def instance_batch(*, write: bool, limit: int | None = None) -> dict:
    """Один проход по кандидатам (с фото). limit — для частичных прогонов."""
    up = await uchet_pool()
    pp = await photos_pool()
    insts = [dict(r) for r in await up.fetch(_CANDIDATES_SQL)]
    with_photos = {r["owner_id"] for r in await pp.fetch(
        """SELECT DISTINCT c.owner_id
             FROM photo_collages c JOIN photos p ON p.collage_id = c.id
            WHERE c.owner_kind = 'instance' AND p.state = 'uploaded'""")}
    queue = [i for i in insts if str(i["id"]) in with_photos]
    if limit:
        queue = queue[:limit]
    sem = asyncio.Semaphore(settings.instance_concurrency)
    counts: dict[str, int] = {}

    async def one(inst: dict) -> None:
        async with sem:
            v = await run_instance(inst, write=write)
            if v:
                counts[v] = counts.get(v, 0) + 1

    await asyncio.gather(*(one(i) for i in queue))
    log.info("instance batch: очередь=%d итоги=%s write=%s",
             len(queue), counts, write)
    return {"queue": len(queue), "counts": counts}


async def instance_loop() -> None:
    mode = settings.instance_mode
    write = mode == "write"
    log.info("instance loop start; mode=%s", mode)
    while True:
        try:
            await instance_batch(write=write)
        except Exception as e:            # noqa: BLE001 — цикл не должен умирать
            log.warning("instance loop: %s: %s", type(e).__name__, e)
        await asyncio.sleep(settings.instance_poll_s)
