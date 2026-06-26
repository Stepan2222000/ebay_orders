"""Фото товаров eBay — скачивание галереи по item_number и заливка в MinIO.

Лёгкий путь (docs/ITEM_PHOTOS.md): ссылки галереи достаёт
``ebay_library.fetch_image_urls`` (один GET, без браузера/прокси), картинки качаем
httpx'ом, льём в MinIO через ``S3Photos``, индекс пишем в ``item_photos`` +
статус в ``listing_photos``. Идемпотентно по ``listing_photos`` (один номер на
многих скриншотах — качаем раз). Конкуренция фото-операций — семафор. Best-effort:
дёргается из OCR-воркера после коммита, его ошибки OCR не валят.

Семантика статуса:
- success (есть/нет картинок) → ``done``;
- ``ParseError`` / ``TransportError`` с 404 (ended/снят) → ``failed`` (не ретраим);
- прочий транзиент (сеть/AccessDenied) → ``pending``, повтор при след. встрече, но
  после ``photo_max_attempts`` неудач — ``failed`` (хватит долбить).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

import ebay_library
import httpx
from ebay_library import S3Photos
from ebay_library.errors import ParseError, TransportError

from .config import settings
from .db import pool

log = logging.getLogger(__name__)

_sem = asyncio.Semaphore(settings.photo_concurrency)
_inflight: set[str] = set()          # дедуп параллельных фетчей одного номера в процессе
_s3: S3Photos | None = None
_http: httpx.AsyncClient | None = None


def _s3_ebay() -> S3Photos:
    global _s3
    if _s3 is None:
        _s3 = S3Photos()                 # дефолт: бакет ebay-data-photos, внешний MinIO
    return _s3


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http


async def _download(url: str) -> bytes:
    r = await _client().get(url)
    r.raise_for_status()
    return r.content


def _is_terminal(err: Exception) -> bool:
    """Мёртвый листинг — не ретраим. 404 у item-транспорта прилетает как
    TransportError('unexpected status 404 ...'); вёрстка уехала — ParseError."""
    if isinstance(err, ParseError):
        return True
    if isinstance(err, TransportError) and "404" in str(err):
        return True
    return False


async def ensure_ebay_photos(item_numbers: list[str]) -> None:
    """Best-effort: для каждого нового номера скачать галерею eBay в MinIO и
    записать в ``item_photos``. Уже ``done``/``failed`` — пропускаем. Не бросает."""
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
                "SELECT ebay_status FROM listing_photos WHERE item_number = $1", item_number
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
        log.warning("ebay photos %s: unexpected %s: %s", item_number, type(e).__name__, e)


async def _fetch_and_store(item_number: str) -> None:
    p = await pool()
    async with p.acquire() as conn:
        attempts = await conn.fetchval(
            """INSERT INTO listing_photos(item_number, ebay_status, attempts)
               VALUES ($1, 'pending', 1)
               ON CONFLICT (item_number)
               DO UPDATE SET attempts = listing_photos.attempts + 1
               RETURNING attempts""",
            item_number,
        )

    try:
        urls = await ebay_library.fetch_image_urls(item_number)
    except Exception as e:                # noqa: BLE001
        terminal = _is_terminal(e) or attempts >= settings.photo_max_attempts
        status = "failed" if terminal else "pending"
        async with p.acquire() as conn:
            await conn.execute(
                """UPDATE listing_photos
                      SET ebay_status = $2, last_error = $3,
                          fetched_at = CASE WHEN $2 = 'failed' THEN now() ELSE fetched_at END
                    WHERE item_number = $1""",
                item_number, status, f"{type(e).__name__}: {str(e)[:300]}",
            )
        log.info("ebay photos %s: %s (%s, attempt %d)",
                 item_number, status, type(e).__name__, attempts)
        return

    # success: качаем и льём (urls пустой = живой листинг без картинок → done с нулём)
    s3 = _s3_ebay()
    uploaded: list[tuple[int, str, bytes, str]] = []
    for idx, url in enumerate(urls):
        content = await _download(url)
        url_hash = hashlib.md5(url.encode()).hexdigest()
        s3_url = await s3.upload_jpeg(f"{item_number}/{url_hash}.jpg", content)
        uploaded.append((idx, url, bytes.fromhex(url_hash), s3_url))

    async with p.acquire() as conn:
        async with conn.transaction():
            for idx, url, uh, s3_url in uploaded:
                await conn.execute(
                    """INSERT INTO item_photos(item_number, source, idx, s3_url, url_hash, ebay_url)
                       VALUES ($1, 'ebay', $2, $3, $4, $5)
                       ON CONFLICT (item_number, source, url_hash) DO NOTHING""",
                    item_number, idx, s3_url, uh, url,
                )
            await conn.execute(
                """UPDATE listing_photos
                      SET ebay_status = 'done', last_error = NULL, fetched_at = now()
                    WHERE item_number = $1""",
                item_number,
            )
    log.info("ebay photos %s: done, %d photos", item_number, len(urls))
