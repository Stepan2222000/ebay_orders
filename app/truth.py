"""Агент истины по артикулам — контур 1 (article_truth/SPEC.md §6).

Мультимодальный агент определяет настоящий состав лота eBay-листинга: все фото
(MinIO) + канонический титул + снапшот-тексты + qty заказов + кандидаты общих
regex-правил + каталожный контекст (smart_fdw, включая состав китов). Модель
одна — gpt-5.6-luna, фолбэка нет: сбой вызова = failed-прогон, ретраи циклом.

Очередь ставит себе сам воркер:
- быстрый цикл: листинги без единого прогона, у которых снапшот И фото
  терминальны (done|failed), нет human-строк и открытой title_mismatch;
- часовой цикл: пересчёт отпечатков входа ВСЕХ нефинальных — так сами
  подхватываются пополнение каталога smart, правка правил и догрузка текстов
  (SPEC §6 «Перепрогоны»). Финальная истина (строки agent/human) автоматически
  не перепрогоняется никогда.

Идемпотентность — отпечаток входа: done-прогон с тем же отпечатком не
повторяется; failed-прогоны ретраятся до truth_max_attempts на отпечаток.

Запись (SPEC §6 «Постобработка», всё в одной транзакции, режим TRUTH_WRITE=1;
без него — сухой режим, пишутся только agent_runs с dry_run=true):
- агент читает, код решает: нормализация прочитанного ЧЕРЕЗ ПРАВИЛА (дилерский
  префикс срезается), гейт «артикул обязан матчиться правилом», лукап в
  smart_fdw.part_articles;
- противоречия/qty-конфликт → conflict + карточка contradiction, записей нет;
- не покрыт правилами → conflict + карточка (номера — в agent_runs, для
  экрана «непокрытые форматы»);
- все позиции в каталоге → замена regex/agent-строк item_parts новыми
  agent-строками (qty агрегируется по детали), match_status='linked';
- хоть одна позиция не в каталоге → not_in_catalog, item_parts не трогаем
  (всё-или-ничего); нет позиций → no_article;
- human-строки неприкосновенны: авто-очередь такие листинги не берёт вовсе.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time

import httpx
from openai import APIError, AsyncOpenAI

from .config import settings
from .db import pool
from .matching import extract_candidates, get_rules

log = logging.getLogger(__name__)

DESC_CAND_CAP = 20        # кандидатов только-из-description сверх капа режем с пометкой
DESC_TEXT_CAP = 3000
PROMPT_REV = "v5"         # аудит: пишется в input_context прогона
# Салт отпечатка ЗАМОРОЖЕН (решение 17.07): правка промпта сама по себе не
# перепрогоняет уже обработанное — новый промпт применяется только при реальном
# изменении входа, у новых листингов и при ручном перепрогоне. Массовый
# перепрогон на новый промпт — осознанное действие (db/rerun_finals.py).
_FP_PROMPT_SALT = "v4"

SCHEMA = {
    "type": "object",
    "properties": {
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article_read": {"type": "string"},
                    "qty": {"type": "integer"},
                    "sources": {"type": "array", "items": {
                        "type": "string", "enum": ["photo", "title", "description", "specifics"]}},
                    "note": {"type": "string"},
                },
                "required": ["article_read", "qty", "sources", "note"],
                "additionalProperties": False,
            },
        },
        "lot_kind": {"type": "string", "enum": ["single", "kit", "bundle", "unknown"]},
        "contradictions": {"type": "array", "items": {"type": "string"},
                           "description": "unresolved contradictions that AFFECT the conclusion; empty if none"},
        "near_articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "where": {"type": "string"},
                               "why": {"type": "string"}},
                "required": ["text", "where", "why"],
                "additionalProperties": False,
            },
        },
        "qty_note": {"type": "string",
                     "description": "empty if order quantities are consistent with the lot"},
        "comment": {"type": "string"},
    },
    "required": ["positions", "lot_kind", "contradictions", "near_articles", "qty_note", "comment"],
    "additionalProperties": False,
}

PROMPT = """You determine the TRUE contents of an eBay listing for a marine parts warehouse.

TASK: from the listing photos and texts below, determine which part number(s) were sold in this lot and in what quantity per lot.

Rules:
- Every position needs an article read from an actual source (label on photo, title, description, item specifics). Never invent numbers.
- A KIT is one product sold as a set: output ONE position with the kit's own article. Numbers of its internal components are NOT separate positions.
- A BUNDLE is several independent parts sold together: one position per part with its qty.
- Sellers list cross-numbers and compatible numbers; those are the same single part — record the form the MANUFACTURER physically put on the part itself or its factory label/box; numbers added by the seller (handwriting, price stickers, title, description) are weaker evidence for which cross-form to record and go to near_articles. This only chooses among cross-numbers of the SAME part — never record an unrelated number just because it appears on a photo.
- COMPLEX CASE — set plus its own component: the lot contains a main part, and a number that CATALOG CONTEXT declares a component of that part's set ("KIT containing") is also visible on THIS listing's photos or in its texts. Whether that component is included in the set or is an extra unit is decided ONLY by an explicit statement in the listing texts ("hub kit included", "comes with ..."): then output ONE position for the set. Without such a statement you MUST output one position for the set AND add a contradiction naming this ambiguity (set with component included vs set + extra component) — a human will resolve it. You MUST NOT treat the following as proof of inclusion: the catalog "KIT containing" line, "compatible with ..." wording, the component's own box being visible on photos, or your general knowledge of how this product normally ships.
- Photo labels are the strongest evidence. Read them carefully, character by character.
- Mercury/Quicksilver numbers are often written with a space before a short trailing group ("805759T 1", "88397A 1", "84-816761a 4"): the trailing 1-2 characters are PART of the number (805759T1, 88397A1, 816761A4), not a quantity — especially when the glued form appears in CATALOG CONTEXT or candidate hints. A bare digit after a P/N is almost never a quantity marker.
- When a photo label shows an aftermarket maker's own number (Walker, Sierra, Martyr, GLM, …) while title/specifics carry the OEM number of the same part — both identify one part: pick the number that exists in CATALOG CONTEXT (usually the OEM one) and keep the other in near_articles as a cross-number.
- contradictions: ONLY unresolved contradictions that affect which article/composition is correct (e.g. specifics MPN vs photo label disagree and you cannot justify a choice). If you resolved it with evidence — explain in note/near_articles instead, do not list here.
- near_articles: ONLY strings that are plausibly PART NUMBERS by meaning and context (cross-numbers, alternate/OEM numbers, internal component numbers) that you did not use, each with a reason. Judge by meaning: barcodes/UPC/EAN digits, dates, years, quantities, prices, zip codes, addresses, packaging/print lot codes, the seller's own stock/inventory/location codes and meaningless title fragments (e.g. a leading batch code like "*NEW OEM* 0810") are NOT part numbers — omit them entirely, do not list them anywhere.
- One lot containing N units of a part is NOT a problem — put N into that position's qty. qty_note is ONLY for the listing's own sources contradicting each other about what one lot contains (e.g. title says "pair", photos clearly show one). The buyer's order quantity ({order_qtys}) is context, not a source of conflict.
- Write all note/why/comment/contradictions texts in Russian.

{title_block}

CATALOG CONTEXT (our internal catalog, may be incomplete — a number absent here can still be a real article):
{catalog}

REGEX CANDIDATE HINTS (auto-extracted from all texts, may contain noise from broad numeric rules):
{candidates}

LISTING DATA:
- Item specifics: {specifics}
- Description: {description}
- Photos attached: {n_photos}{photo_note}
"""

_llm: AsyncOpenAI | None = None
_http: httpx.AsyncClient | None = None


def _llm_client() -> AsyncOpenAI:
    global _llm
    if _llm is None:
        _llm = AsyncOpenAI(base_url=settings.openai_base_url,
                           api_key=settings.openai_api_key,
                           timeout=settings.truth_llm_timeout_s)
    return _llm


def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


# ─── Сбор входа и отпечаток ──────────────────────────────────────────────────

def _fingerprint(inp: dict, *, catalog: str | None = None) -> str:
    """Отпечаток входа. catalog-override — детекция «менялся только каталог»
    (SPEC §6): отпечаток свежего входа со СТАРЫМ каталожным контекстом совпал
    со старым отпечатком ⇒ чтение-вход не менялся, применим перерешив без LLM."""
    d = {k: inp[k] for k in ("title", "specifics", "description", "qtys", "candidates")}
    d["catalog"] = inp["catalog"] if catalog is None else catalog
    d |= {"photo_hashes": [p["url_hash"] for p in inp["photos"]],
          "model": settings.truth_model, "prompt_rev": _FP_PROMPT_SALT}
    return hashlib.sha256(json.dumps(
        d, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def gather_input(conn, item_number: str) -> dict | None:
    """Полный вход агента для листинга. None — листинг не существует."""
    item = await conn.fetchrow(
        "SELECT item_number, item_title, match_status FROM items WHERE item_number = $1",
        item_number)
    if item is None:
        return None
    snap = await conn.fetchrow(
        "SELECT status, title, specifics, specifics_raw, description FROM item_snapshots "
        "WHERE item_number = $1", item_number)
    card = await conn.fetchrow(
        "SELECT payload FROM review_cards WHERE kind = 'title_mismatch' AND status = 'open' "
        "AND item_number = $1", item_number)
    qtys = [r["item_quantity"] for r in await conn.fetch(
        "SELECT item_quantity FROM order_items WHERE item_number = $1 ORDER BY order_id",
        item_number)]
    photos = [dict(r) for r in await conn.fetch(
        "SELECT s3_url, encode(url_hash, 'hex') AS url_hash FROM item_photos "
        "WHERE item_number = $1 ORDER BY (source = 'manual'), idx LIMIT $2",
        item_number, settings.truth_max_photos)]

    title = item["item_title"]
    specifics = ""
    description = ""
    if snap:
        if snap["specifics"]:
            specifics = json.dumps(snap["specifics"], ensure_ascii=False)
        if snap["specifics_raw"]:
            specifics = (specifics + "\nRAW: " + snap["specifics_raw"]).strip()
        description = (snap["description"] or "")[:DESC_TEXT_CAP]

    # Правила правятся напрямую в БД (SPEC §8) — кеш процесса без refresh
    # слеп к правкам до рестарта; каждый прогон стартует со свежих правил.
    rules = await get_rules(conn, refresh=True)
    texts = {"title": title, "specifics": specifics, "description": description}
    cand_src: dict[str, set[str]] = {}
    for src, t in texts.items():
        for c in extract_candidates(t or "", rules):
            cand_src.setdefault(c, set()).add(src)
    desc_only = sorted(c for c, s in cand_src.items() if s == {"description"})
    dropped = 0
    if len(desc_only) > DESC_CAND_CAP:
        for c in desc_only[DESC_CAND_CAP:]:
            del cand_src[c]
            dropped += 1

    catalog_lines, kit_bom = [], {}
    if cand_src:
        rows = await conn.fetch(
            """SELECT upper(pa.article) art, p.id, p.name, p.articles
                 FROM smart_fdw.part_articles pa JOIN smart_fdw.parts p ON p.id = pa.part_id
                WHERE upper(pa.article) = ANY($1::text[])""", list(cand_src))
        seen = set()
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            comps = await conn.fetch(
                """SELECT p2.name, pc.quantity, p2.articles
                     FROM smart_fdw.part_components pc
                     JOIN smart_fdw.parts p2 ON p2.id = pc.child_id
                    WHERE pc.parent_id = $1""", r["id"])
            line = f"- {r['id']} \"{r['name']}\" cross-numbers: {', '.join(r['articles'] or [])}"
            if comps:
                line += "\n  KIT containing: " + "; ".join(
                    f"{c['name']} x{c['quantity']} (articles: {', '.join(c['articles'] or [])})"
                    for c in comps)
            catalog_lines.append(line)
        known = {r["art"] for r in rows}
        unknown = sorted(c for c in cand_src if c not in known)
        if unknown:
            catalog_lines.append(f"- NOT in catalog: {', '.join(unknown)}")

    inp = {
        "item_number": item_number,
        "match_status": item["match_status"],
        "title": title,
        "title_card": dict(card["payload"]) if card else None,
        "specifics": specifics,
        "description": description,
        "qtys": qtys or [1],
        "photos": photos,
        "candidates": {c: sorted(s) for c, s in sorted(cand_src.items())},
        "dropped_desc_candidates": dropped,
        "catalog": "\n".join(catalog_lines) or "(no candidates matched catalog)",
    }
    inp["fingerprint"] = _fingerprint(inp)
    return inp


# ─── Вызов модели ────────────────────────────────────────────────────────────

async def _call_llm(inp: dict) -> tuple[dict, dict, str]:
    """→ (разобранный ответ, usage, model). Исключения летят наружу (failed-прогон)."""
    if inp["title_card"]:
        title_block = ("TITLES (WARNING: order-screenshot title and listing-page title DISAGREE,"
                       " treat carefully):\n"
                       f"- from order screenshot (OCR): {inp['title_card']['ocr_title']}\n"
                       f"- from listing page: {inp['title_card']['pdp_title']}")
    else:
        title_block = f"TITLE: {inp['title']}"
    cand_text = "\n".join(f"- {c} (seen in: {', '.join(s)})"
                          for c, s in inp["candidates"].items()) or "(none)"
    if inp["dropped_desc_candidates"]:
        cand_text += (f"\n({inp['dropped_desc_candidates']} more description-only numeric "
                      f"candidates dropped as noise)")
    content: list[dict] = [{"type": "text", "text": PROMPT.format(
        order_qtys=inp["qtys"], title_block=title_block, catalog=inp["catalog"],
        candidates=cand_text,
        specifics=inp["specifics"] or "(нет — листинг мёртв, снапшота нет)",
        description=inp["description"] or "(нет)",
        n_photos=len(inp["photos"]),
        photo_note="" if inp["photos"] else " (ФОТО НЕТ — работай только по текстам)")}]
    for p in inp["photos"]:
        img = (await _http_client().get(p["s3_url"])).content
        mime = "image/png" if p["s3_url"].endswith(".png") else "image/jpeg"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(img).decode()}"}})

    resp = await _llm_client().chat.completions.create(
        model=settings.truth_model,
        messages=[{"role": "user", "content": content}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "lot_truth", "strict": True, "schema": SCHEMA}},
        max_tokens=settings.truth_max_tokens,
    )
    ans = json.loads(resp.choices[0].message.content)
    usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
             "completion_tokens": getattr(resp.usage, "completion_tokens", None)}
    return ans, usage, resp.model or settings.truth_model


# ─── Постобработка: агент читает, код решает ─────────────────────────────────

async def postprocess(conn, ans: dict) -> tuple[str, list[dict]]:
    """Нормализация через правила → гейт → лукап → вердикт (SPEC §6).

    Лукап пробует и СЫРУЮ форму (каталог хранит номера вроде «816811-1» как
    есть — каталог тоже «паспорт» номера), и кандидатов правил; каноном
    становится каталожная форма. Гейт правил остаётся для всего, чего в
    каталоге нет."""
    rules = await get_rules(conn)
    positions, uncovered = [], []
    for p in ans["positions"]:
        raw = (p["article_read"] or "").strip().upper()
        cands = extract_candidates(p["article_read"], rules)
        tries = [raw, raw.replace(" ", "")] + sorted(cands, key=len, reverse=True)
        best, part = None, None
        for c in dict.fromkeys(t for t in tries if t):
            row = await conn.fetchrow(
                "SELECT pa.article, pa.part_id, p.name FROM smart_fdw.part_articles pa "
                "JOIN smart_fdw.parts p ON p.id = pa.part_id WHERE upper(pa.article) = $1", c)
            if row:
                best, part = row["article"], dict(row)
                break
        if part is None and not cands:
            uncovered.append(p["article_read"])
            positions.append({**p, "canonical": None, "part_id": None, "part_name": None,
                              "gate": "uncovered"})
            continue
        if best is None:
            best = max(cands, key=len)
        positions.append({**p, "canonical": best,
                          "part_id": part["part_id"] if part else None,
                          "part_name": part["name"] if part else None, "gate": "ok"})

    if ans["contradictions"] or ans["qty_note"]:
        verdict = "conflict"
    elif uncovered:
        verdict = "conflict"          # формат не покрыт правилами — громкий разбор
    elif not positions:
        verdict = "no_article"
    elif all(p["part_id"] for p in positions):
        verdict = "linked"
    else:
        verdict = "not_in_catalog"
    return verdict, positions


async def apply_truth(conn, item_number: str, verdict: str, positions: list[dict],
                      ans: dict) -> None:
    """Запись истины (вызывать внутри транзакции). Human-строки неприкосновенны —
    их наличие здесь означает гонку с ручной правкой: не пишем, карточка."""
    human_rows = await conn.fetch(
        "SELECT part_id, quantity FROM item_parts WHERE item_number = $1 "
        "AND match_method = 'human'", item_number)
    if human_rows:
        agent_agg: dict[str, int] = {}
        for p in positions:
            if p.get("part_id"):
                agent_agg[p["part_id"]] = agent_agg.get(p["part_id"], 0) + max(1, p["qty"])
        if verdict == "linked" and agent_agg == {r["part_id"]: r["quantity"]
                                                 for r in human_rows}:
            return                      # агент согласен с human — не шумим
        await _open_card(conn, "human_disagreement", item_number,
                         {"verdict": verdict,
                          "positions": [{k: p.get(k) for k in
                                         ("canonical", "part_id", "qty")} for p in positions],
                          "comment": ans.get("comment", "")})
        return

    # Агент главнее regex (SPEC §3): его вердикт всегда замещает предварительные
    # строки — в т.ч. при not_in_catalog/no_article/conflict, иначе intake видел бы
    # уверенный regex-артикул при статусе «истина не готова».
    await conn.execute(
        "DELETE FROM item_parts WHERE item_number = $1 "
        "AND match_method IN ('regex_exact', 'agent')", item_number)

    if verdict != "conflict":
        # причина снята новым прогоном — открытая конфликт-карточка закрывается сама
        await conn.execute(
            """UPDATE review_cards
                  SET status = 'resolved', resolved_at = now(),
                      resolution = 'закрыт новым прогоном агента (вердикт: ' || $2 || ')'
                WHERE kind = 'contradiction' AND item_number = $1 AND status = 'open'""",
            item_number, verdict)

    if verdict == "linked":
        agg: dict[str, dict] = {}
        for p in positions:
            a = agg.setdefault(p["part_id"], {"article": p["canonical"], "qty": 0})
            a["qty"] += max(1, p["qty"])
        for part_id, a in agg.items():
            await conn.execute(
                """INSERT INTO item_parts(item_number, part_id, quantity, matched_article,
                                          match_method)
                   VALUES ($1, $2, $3, $4, 'agent')""",
                item_number, part_id, a["qty"], a["article"])
        note = "agent: " + "; ".join(f"{a['article']}×{a['qty']}" for a in agg.values())
    elif verdict == "conflict":
        payload = {"contradictions": ans["contradictions"], "qty_note": ans["qty_note"],
                   "uncovered": [p["article_read"] for p in positions
                                 if p["gate"] == "uncovered"],
                   "comment": ans.get("comment", "")}
        await _open_card(conn, "contradiction", item_number, payload)
        note = "agent: конфликт — " + (
            "; ".join(ans["contradictions"]) or ans["qty_note"] or "формат не покрыт правилами")
    else:  # not_in_catalog | no_article — item_parts не трогаем (всё-или-ничего)
        missing = [p["canonical"] or p["article_read"] for p in positions if not p["part_id"]]
        note = ("agent: состав прочитан, нет в каталоге: " + "; ".join(missing)
                ) if verdict == "not_in_catalog" else "agent: артикула нет ни в одном источнике"
    await conn.execute(
        "UPDATE items SET match_status = $2, matched_at = now(), match_note = $3 "
        "WHERE item_number = $1", item_number, verdict, note[:500])


async def _open_card(conn, kind: str, item_number: str, payload: dict) -> None:
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM review_cards WHERE kind = $1 AND item_number = $2 "
        "AND status = 'open')", kind, item_number)
    if not exists:
        await conn.execute(
            "INSERT INTO review_cards(kind, item_number, payload) VALUES ($1, $2, $3)",
            kind, item_number, payload)


# ─── Прогон одного листинга ──────────────────────────────────────────────────

async def run_listing(item_number: str, *, write: bool) -> dict | None:
    """Полный цикл: вход → LLM → постобработка → (запись). Возвращает итог прогона."""
    p = await pool()
    async with p.acquire() as conn:
        inp = await gather_input(conn, item_number)
        if inp is None:
            return None
        run_id = await conn.fetchval(
            """INSERT INTO agent_runs(item_number, input_fingerprint, model, status,
                                      dry_run, input_context)
               VALUES ($1, $2, $3, 'running', $4, $5) RETURNING id""",
            item_number, inp["fingerprint"], settings.truth_model, not write,
            {"candidates": inp["candidates"], "catalog": inp["catalog"],
             "n_photos": len(inp["photos"]), "qtys": inp["qtys"],
             "prompt_rev": PROMPT_REV})
    try:
        ans, usage, model = await _call_llm(inp)
    except (APIError, httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        async with p.acquire() as conn:
            await conn.execute(
                "UPDATE agent_runs SET status = 'failed', error = $2, finished_at = now() "
                "WHERE id = $1", run_id, f"{type(e).__name__}: {str(e)[:400]}")
        log.warning("truth %s: failed — %s: %s", item_number, type(e).__name__, e)
        return {"item_number": item_number, "verdict": "failed"}

    async with p.acquire() as conn:
        async with conn.transaction():
            verdict, positions = await postprocess(conn, ans)
            if write:
                await apply_truth(conn, item_number, verdict, positions, ans)
            await conn.execute(
                """UPDATE agent_runs
                      SET status = 'done', raw_response = $2, positions = $3,
                          near_articles = $4, contradictions = $5, qty_note = $6,
                          verdict = $7, model = $8, prompt_tokens = $9,
                          completion_tokens = $10, finished_at = now()
                    WHERE id = $1""",
                run_id, ans, positions, ans["near_articles"], ans["contradictions"],
                ans["qty_note"], verdict, model,
                usage["prompt_tokens"], usage["completion_tokens"])
    log.info("truth %s: %s (%s, %d позиций, write=%s)",
             item_number, verdict, ans["lot_kind"], len(positions), write)
    return {"item_number": item_number, "verdict": verdict, "positions": positions,
            "lot_kind": ans["lot_kind"], "run_id": run_id}


async def redecide_listing(item_number: str, *, write: bool) -> dict:
    """Перерешив без LLM (SPEC §6): чтение старое, решение свежее.

    Применим, когда со времени последнего done-прогона изменился ТОЛЬКО
    каталожный контекст («агент читает — код решает»: пополнение каталога не
    меняет прочитанного, перечитывать лот LLM-ом незачем). Пишет обычную
    done-строку прогона с новым отпечатком — идемпотентность, часовой пересчёт
    и авто-закрытие карточек работают без изменений."""
    p = await pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            inp = await gather_input(conn, item_number)
            if inp is None:
                return {"applicable": False, "reason": "no_input"}
            run = await conn.fetchrow(
                """SELECT id, input_fingerprint, input_context, raw_response, verdict, model
                     FROM agent_runs
                    WHERE item_number = $1 AND status = 'done' AND NOT dry_run
                    ORDER BY created_at DESC LIMIT 1""", item_number)
            if run is None:
                return {"applicable": False, "reason": "no_run"}
            if inp["fingerprint"] == run["input_fingerprint"]:
                return {"applicable": False, "reason": "unchanged",
                        "verdict": run["verdict"]}
            old_catalog = (run["input_context"] or {}).get("catalog")
            if (old_catalog is None
                    or _fingerprint(inp, catalog=old_catalog) != run["input_fingerprint"]):
                return {"applicable": False, "reason": "reading_changed"}

            ans = run["raw_response"]
            verdict, positions = await postprocess(conn, ans)
            if write:
                await apply_truth(conn, item_number, verdict, positions, ans)
            await conn.execute(
                """INSERT INTO agent_runs(item_number, input_fingerprint, model, status,
                                          dry_run, raw_response, positions, near_articles,
                                          contradictions, qty_note, verdict, input_context,
                                          finished_at)
                   VALUES ($1, $2, $3, 'done', $4, $5, $6, $7, $8, $9, $10, $11, now())""",
                item_number, inp["fingerprint"], run["model"], not write,
                ans, positions, ans["near_articles"], ans["contradictions"], ans["qty_note"],
                verdict,
                {"candidates": inp["candidates"], "catalog": inp["catalog"],
                 "n_photos": len(inp["photos"]), "qtys": inp["qtys"],
                 "redecided_from_run": run["id"],
                 "prompt_rev": (run["input_context"] or {}).get("prompt_rev")})
            missing = [q["canonical"] or q["article_read"]
                       for q in positions if not q["part_id"]]
            log.info("truth %s: перерешив без LLM — %s (по чтению прогона %d, write=%s)",
                     item_number, verdict, run["id"], write)
            return {"applicable": True, "verdict": verdict, "was": run["verdict"],
                    "missing": missing}


# ─── Очередь ─────────────────────────────────────────────────────────────────

_ELIGIBLE_SQL = """
SELECT i.item_number
  FROM items i
  JOIN item_snapshots s ON s.item_number = i.item_number AND s.status IN ('done', 'failed')
  JOIN listing_photos lp ON lp.item_number = i.item_number
                        AND lp.ebay_status IN ('done', 'failed')
 WHERE NOT EXISTS (SELECT 1 FROM item_parts ip
                    WHERE ip.item_number = i.item_number
                      AND ip.match_method IN ('agent', 'human'))
   AND NOT EXISTS (SELECT 1 FROM review_cards rc
                    WHERE rc.item_number = i.item_number
                      AND rc.kind = 'title_mismatch' AND rc.status = 'open')
   AND i.match_status <> 'conflict'
"""
# conflict исключён из быстрого цикла (он «has runs» по построению), но часовой
# пересчёт отпечатков его подхватит при изменении входа — добавляется ниже.

_FRESH_SQL = _ELIGIBLE_SQL + """
   AND NOT EXISTS (SELECT 1 FROM agent_runs ar WHERE ar.item_number = i.item_number)
 ORDER BY i.item_number
"""

_RECHECK_SQL = """
SELECT i.item_number
  FROM items i
  JOIN item_snapshots s ON s.item_number = i.item_number AND s.status IN ('done', 'failed')
  JOIN listing_photos lp ON lp.item_number = i.item_number
                        AND lp.ebay_status IN ('done', 'failed')
 WHERE NOT EXISTS (SELECT 1 FROM item_parts ip
                    WHERE ip.item_number = i.item_number
                      AND ip.match_method IN ('agent', 'human'))
   AND NOT EXISTS (SELECT 1 FROM review_cards rc
                    WHERE rc.item_number = i.item_number
                      AND rc.kind = 'title_mismatch' AND rc.status = 'open')
 ORDER BY i.item_number
"""


async def _should_run(conn, item_number: str, *, write: bool) -> bool:
    """Пересчитать отпечаток и решить, нужен ли прогон (идемпотентность + ретраи)."""
    inp = await gather_input(conn, item_number)
    if inp is None:
        return False
    done = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM agent_runs
            WHERE item_number = $1 AND input_fingerprint = $2 AND status = 'done'
              AND (dry_run = false OR $3::boolean = false))""",
        item_number, inp["fingerprint"], write)
    if done:
        return False
    fails = await conn.fetchval(
        "SELECT count(*) FROM agent_runs WHERE item_number = $1 "
        "AND input_fingerprint = $2 AND status = 'failed'",
        item_number, inp["fingerprint"])
    return fails < settings.truth_max_attempts


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    write = settings.truth_write
    log.info("truth worker start; model=%s write=%s concurrency=%d",
             settings.truth_model, write, settings.truth_concurrency)
    sem = asyncio.Semaphore(settings.truth_concurrency)
    running: set[asyncio.Task] = set()
    last_recheck = 0.0

    async def one(num: str) -> None:
        async with sem:
            p = await pool()
            async with p.acquire() as conn:
                need = await _should_run(conn, num, write=write)
            if not need:
                return
            # сначала дешёвый перерешив (менялся только каталог — без LLM);
            # LLM — только когда менялось само чтение-вход
            r = await redecide_listing(num, write=write)
            if not r["applicable"] and r["reason"] != "unchanged":
                await run_listing(num, write=write)

    while True:
        try:
            p = await pool()
            async with p.acquire() as conn:
                nums = [r["item_number"] for r in await conn.fetch(_FRESH_SQL)]
                now = time.monotonic()
                if now - last_recheck >= settings.truth_recheck_period_s:
                    last_recheck = now
                    # правила могли поправить из UI — кеш процесса обновляем,
                    # иначе отпечатки не заметят изменения
                    await get_rules(conn, refresh=True)
                    nums += [r["item_number"] for r in await conn.fetch(_RECHECK_SQL)]
            queued = {t.get_name() for t in running}
            for num in dict.fromkeys(nums):
                if num in queued:
                    continue
                t = asyncio.create_task(one(num), name=num)
                running.add(t)
                t.add_done_callback(running.discard)
        except Exception as e:                # noqa: BLE001 — цикл не должен умирать
            log.warning("truth loop: %s: %s", type(e).__name__, e)
        await asyncio.sleep(settings.truth_poll_s)
