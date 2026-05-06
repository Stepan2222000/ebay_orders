from __future__ import annotations

import asyncio
import logging

import asyncpg

from app.ocr_client import OcrClient

log = logging.getLogger("stage_a")


_CLAIM_SQL = """
WITH p AS (
    SELECT sha256
      FROM screenshots
     WHERE ocr_status = 'pending'
     ORDER BY created_at
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE screenshots
   SET ocr_status = 'running',
       updated_at = now()
  FROM p
 WHERE screenshots.sha256 = p.sha256
RETURNING screenshots.sha256, screenshots.bytes
"""


async def _claim_one(pool: asyncpg.Pool) -> tuple[str, bytes] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_CLAIM_SQL)
    if row is None:
        return None
    return row["sha256"], row["bytes"]


async def _save_done(
    pool: asyncpg.Pool,
    sha256: str,
    model: str,
    payload: dict,
    recognition_ms: int,
) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO raw_ocr (sha256, model, payload, recognition_ms)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (sha256) DO UPDATE
                   SET model = EXCLUDED.model,
                       payload = EXCLUDED.payload,
                       recognition_ms = EXCLUDED.recognition_ms,
                       recognized_at = now()
                """,
                sha256,
                model,
                payload,
                recognition_ms,
            )
            await conn.execute(
                "UPDATE screenshots SET ocr_status='done', error_text=NULL WHERE sha256=$1",
                sha256,
            )


async def _save_failed(pool: asyncpg.Pool, sha256: str, error_text: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE screenshots SET ocr_status='failed', error_text=$2 WHERE sha256=$1",
            sha256,
            error_text[:1000],
        )


async def _process_one(
    pool: asyncpg.Pool,
    client: OcrClient,
    sem: asyncio.Semaphore,
    model: str,
    sha256: str,
    png: bytes,
) -> None:
    try:
        try:
            payload, ms = await client.recognize(png)
        except Exception as e:  # noqa: BLE001 — собираем любую ошибку модели
            log.warning("stage_a recognize failed sha=%s err=%s", sha256[:12], e)
            await _save_failed(pool, sha256, f"{type(e).__name__}: {e}")
            return
        await _save_done(pool, sha256, model, payload.model_dump(), ms)
        log.info(
            "stage_a done sha=%s ms=%d order_details=%s",
            sha256[:12],
            ms,
            payload.is_order_details,
        )
    finally:
        sem.release()


async def stage_a_loop(
    pool: asyncpg.Pool,
    client: OcrClient,
    concurrency: int,
    poll_seconds: float,
    model: str,
    stop: asyncio.Event,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    log.info("stage_a loop started concurrency=%d poll=%.2fs model=%s", concurrency, poll_seconds, model)
    while not stop.is_set():
        await sem.acquire()
        claimed = await _claim_one(pool)
        if claimed is None:
            sem.release()
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
            continue
        sha256, png = claimed
        asyncio.create_task(_process_one(pool, client, sem, model, sha256, png))
    log.info("stage_a loop exiting")
