"""Снапшоты текстов листинга — title/condition/specifics/description на item_number.

Истина по артикулам (article_truth/SPEC.md §5): тексты объявления нужны агенту,
а страницы умирают — снимаем при первой встрече номера (после OCR, до
save_order; поэтому item_snapshots без FK на items, как item_photos). Транспорт —
лёгкий PDP-парс ``ebay_library.fetch_item_page`` (itm.ebaydesc.com, без браузера).
Best-effort из OCR-воркера: ошибки не валят OCR. Один снапшот навсегда;
обновление — только явным refetch (API «перекачать»).

Исходы (SPEC §5):
- полный PDP → ``done`` (source='ebay');
- 404 → ``failed`` навсегда (страницы нет; тексты — ручной догрузкой через API);
- делистнут с 301 на каталожную /p/{id} → ``failed`` + catalog_url
  (каталожную страницу НЕ парсим — только подсказка человеку);
- прочий транзиент → ``pending``, ретрай при следующей встрече номера/refetch,
  после snapshot_max_attempts — ``failed``.

Сверка титулов — ``reconcile_titles()``, отложенный шаг (зовёт воркер
периодически): совпало (пробелы схлопнуты, регистр не важен; допуск —
OCR-титул, обрезанный скриншотом на «…», как префикс PDP) → items.item_title
перезаписывается точной PDP-формой навсегда; расхождение → карточка
``title_mismatch``, агент по листингу блокируется до разбора. Выполненная
сверка отмечается ``title_checked_at``.
"""
from __future__ import annotations

import asyncio
import logging
import re

from ebay_library.errors import ParseError, TransportError
from ebay_library.http.fetch import make_item_session
from ebay_library.item import fetch_item_page

from .config import settings
from .db import pool

log = logging.getLogger(__name__)

_sem = asyncio.Semaphore(settings.snapshot_concurrency)
_inflight: set[str] = set()          # дедуп параллельных фетчей одного номера в процессе
_session = None                      # одна долгоживущая сессия на процесс (как в ebay_library)


def _sess():
    global _session
    if _session is None:
        _session = make_item_session(max_clients=settings.snapshot_concurrency)
    return _session


async def ensure_snapshots(item_numbers: list[str]) -> None:
    """Best-effort: снапшот для каждого нового номера. done/failed — пропуск. Не бросает."""
    seen: list[str] = []
    for n in item_numbers:
        s = str(n).strip() if n is not None else ""
        if s and s not in seen:
            seen.append(s)
    if seen:
        await asyncio.gather(*(_one(n) for n in seen), return_exceptions=True)


async def _one(item_number: str) -> None:
    if item_number in _inflight:
        return
    try:
        p = await pool()
        async with p.acquire() as conn:
            st = await conn.fetchval(
                "SELECT status FROM item_snapshots WHERE item_number = $1", item_number
            )
        if st in ("done", "failed"):
            return
        _inflight.add(item_number)
        try:
            async with _sem:
                await _fetch_and_store(item_number)
        finally:
            _inflight.discard(item_number)
    except Exception as e:                # noqa: BLE001 — best-effort, не валим OCR
        log.warning("snapshot %s: unexpected %s: %s", item_number, type(e).__name__, e)


async def _fetch_and_store(item_number: str) -> None:
    p = await pool()
    async with p.acquire() as conn:
        attempts = await conn.fetchval(
            """INSERT INTO item_snapshots(item_number, status, attempts)
               VALUES ($1, 'pending', 1)
               ON CONFLICT (item_number)
               DO UPDATE SET attempts = item_snapshots.attempts + 1, updated_at = now()
               RETURNING attempts""",
            item_number,
        )

    try:
        page = await fetch_item_page(_sess(), item_number)
    except (ParseError, TransportError) as e:
        await _store_failure(item_number, e, attempts)
        return

    async with p.acquire() as conn:
        await conn.execute(
            """UPDATE item_snapshots
                  SET status = 'done', source = 'ebay', title = $2, condition = $3,
                      specifics = $4, description = $5, catalog_url = NULL,
                      last_error = NULL, fetched_at = now(), updated_at = now()
                WHERE item_number = $1""",
            item_number, page.title, page.condition,
            page.specifics or {}, page.description or "",
        )
    log.info("snapshot %s: done (%s)", item_number, page.status)


async def _store_failure(item_number: str, e: Exception, attempts: int) -> None:
    err = f"{type(e).__name__}: {str(e)[:300]}"
    catalog_url: str | None = None
    if isinstance(e, TransportError) and "status 404" in str(e):
        status = "failed"                 # страницы нет безвозвратно — не ретраим
    elif isinstance(e, ParseError):
        # делистнут (301 на каталог) либо вёрстка уехала — терминально в обоих случаях
        status = "failed"
        catalog_url = await _catalog_redirect(item_number)
    else:
        status = "failed" if attempts >= settings.snapshot_max_attempts else "pending"

    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            """UPDATE item_snapshots
                  SET status = $2, last_error = $3,
                      catalog_url = COALESCE($4, catalog_url),
                      fetched_at = CASE WHEN $2 = 'failed' THEN now() ELSE fetched_at END,
                      updated_at = now()
                WHERE item_number = $1""",
            item_number, status, err, catalog_url,
        )
    log.info("snapshot %s: %s (attempt %d)%s",
             item_number, status, attempts, " +catalog_url" if catalog_url else "")


async def _catalog_redirect(item_number: str) -> str | None:
    """Куда 301-ит itm-страница. ebay.com/p/… = делистнут в каталог (SPEC §5)."""
    try:
        r = await _sess().get(
            f"https://itm.ebaydesc.com/itm/{item_number}", allow_redirects=False
        )
        loc = r.headers.get("location", "")
    except Exception:                     # noqa: BLE001 — вспомогательная проба, не критично
        return None
    return loc if "ebay.com/p/" in loc else None


# ─── Сверка титулов (SPEC §5 «Титул — храним один») ─────────────────────────

_WS = re.compile(r"\s+")


def _norm(t: str | None) -> str:
    return _WS.sub(" ", t or "").strip().casefold()


def _titles_match(ocr_n: str, pdp_n: str) -> bool:
    """Нормализованные титулы. Допуск ровно один: OCR-титул, обрезанный
    скриншотом на «…»/«...», — как префикс PDP. Остальное — расхождение."""
    if ocr_n == pdp_n:
        return True
    for ell in ("…", "..."):
        if ocr_n.endswith(ell):
            base = ocr_n[: -len(ell)].rstrip()
            return bool(base) and pdp_n.startswith(base)
    return False


async def reconcile_titles() -> None:
    """Отложенная сверка: done-снапшоты с непроверенным титулом × уже
    существующие items-строки. Совпало → канон = точная PDP-форма;
    нет → карточка title_mismatch (одна открытая на листинг)."""
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            """SELECT s.item_number, s.title AS pdp, i.item_title AS ocr
                 FROM item_snapshots s
                 JOIN items i USING (item_number)
                WHERE s.status = 'done' AND s.title IS NOT NULL
                  AND s.title_checked_at IS NULL"""
        )
        for r in rows:
            num, pdp, ocr = r["item_number"], r["pdp"], r["ocr"]
            async with conn.transaction():
                if _titles_match(_norm(ocr), _norm(pdp)):
                    if ocr != pdp:
                        await conn.execute(
                            "UPDATE items SET item_title = $2 WHERE item_number = $1",
                            num, pdp,
                        )
                    log.info("title %s: ок, канон = PDP", num)
                else:
                    exists = await conn.fetchval(
                        """SELECT EXISTS(SELECT 1 FROM review_cards
                            WHERE kind = 'title_mismatch' AND item_number = $1
                              AND status = 'open')""",
                        num,
                    )
                    if not exists:
                        await conn.execute(
                            """INSERT INTO review_cards(kind, item_number, payload)
                               VALUES ('title_mismatch', $1, $2)""",
                            num, {"ocr_title": ocr, "pdp_title": pdp},
                        )
                    log.warning("title %s: РАСХОЖДЕНИЕ — карточка title_mismatch", num)
                await conn.execute(
                    "UPDATE item_snapshots SET title_checked_at = now(), updated_at = now() "
                    "WHERE item_number = $1",
                    num,
                )


async def reconcile_titles_safe() -> None:
    try:
        await reconcile_titles()
    except Exception as e:                # noqa: BLE001 — периодический вызов из воркера
        log.warning("reconcile_titles: %s: %s", type(e).__name__, e)


async def refetch_now(item_number: str) -> dict:
    """«Перекачать» (SPEC §5): сброс к pending и немедленный фетч с пересверкой титула."""
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            """INSERT INTO item_snapshots(item_number, status)
               VALUES ($1, 'pending')
               ON CONFLICT (item_number) DO UPDATE
                  SET status = 'pending', attempts = 0, title_checked_at = NULL,
                      last_error = NULL, updated_at = now()""",
            item_number,
        )
    await _fetch_and_store(item_number)
    await reconcile_titles()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM item_snapshots WHERE item_number = $1", item_number
        )
    return dict(row)
