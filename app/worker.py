"""Воркер.

Обязанности:
  1) стадия A — забирает pending screenshots, отдаёт vision-модели, пишет raw_ocr;
  2) после паузы в работе стадии A — авто-trigger стадии B на накопившиеся
     обработанные снимки.
"""
import asyncio
import logging
import time

import httpx

from .agent import maybe_run_auto_trigger
from .config import settings
from .db import pool
from .ocr import OcrError, transcribe

log = logging.getLogger(__name__)


_CLAIM_SQL = """
WITH claimed AS (
    SELECT sha256
      FROM screenshots
     WHERE ocr_status = 'pending'
     ORDER BY created_at
     FOR UPDATE SKIP LOCKED
     LIMIT $1
)
UPDATE screenshots s
   SET ocr_status = 'running'
  FROM claimed c
 WHERE s.sha256 = c.sha256
RETURNING s.sha256, s.bytes, s.mime_type;
"""

_INSERT_OCR_SQL = """
INSERT INTO raw_ocr(sha256, model, raw_json) VALUES ($1, $2, $3);
"""

_MARK_DONE_SQL = """
UPDATE screenshots
   SET ocr_status = 'done', last_error = NULL
 WHERE sha256 = $1;
"""

_MARK_FAILED_SQL = """
UPDATE screenshots
   SET ocr_status = 'failed', last_error = $2
 WHERE sha256 = $1;
"""

# после этой длительности простоя стадии A (без новых готовых снимков) запускаем агента
_AUTO_TRIGGER_IDLE_S = 5.0


async def _process(http: httpx.AsyncClient, sha: bytes, image_bytes: bytes, mime: str) -> None:
    short = sha.hex()[:12]
    try:
        res = await transcribe(image_bytes, mime, http)
    except OcrError as e:
        async with (await pool()).acquire() as conn:
            await conn.execute(_MARK_FAILED_SQL, sha, str(e)[:1000])
        log.error("ocr fail %s: %s", short, e)
        return

    async with (await pool()).acquire() as conn:
        async with conn.transaction():
            await conn.execute(_INSERT_OCR_SQL, sha, res.model, res.raw_json)
            await conn.execute(_MARK_DONE_SQL, sha)
    obs = (res.raw_json.get("observed") or {})
    log.info(
        "ocr ok   %s  lat=%.1fs  cost=%s  is_order=%s  order_number=%s",
        short, res.latency_s,
        f"${res.cost_usd:.4f}" if res.cost_usd is not None else "?",
        res.raw_json.get("is_order_details"),
        obs.get("order_number"),
    )


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    n = settings.worker_concurrency
    log.info("worker start; ocr_concurrency=%d, auto_trigger_idle=%.1fs", n, _AUTO_TRIGGER_IDLE_S)

    # Стадия A — пул consumer-задач: пока есть свободный слот, забираем
    # следующий pending screenshot и запускаем _process в фоне.
    # Стадия B (auto-trigger) живёт в отдельной фоновой task — пока агент думает,
    # main loop продолжает раздавать OCR новых скриншотов.
    running: set[asyncio.Task] = set()
    agent_task: asyncio.Task | None = None
    last_progress = time.monotonic()

    async def safe_auto_trigger(p):
        try:
            await maybe_run_auto_trigger(p, http)
        except Exception as e:
            log.error("auto-trigger failed: %s", e)

    async with httpx.AsyncClient() as http:
        while True:
            free = n - len(running)
            rows: list = []
            if free > 0:
                async with (await pool()).acquire() as conn:
                    rows = await conn.fetch(_CLAIM_SQL, free)
                for r in rows:
                    t = asyncio.create_task(_process(http, r["sha256"], r["bytes"], r["mime_type"]))
                    running.add(t)
                    t.add_done_callback(running.discard)

            if rows or running:
                last_progress = time.monotonic()
            else:
                idle = time.monotonic() - last_progress
                if idle >= _AUTO_TRIGGER_IDLE_S and (agent_task is None or agent_task.done()):
                    p = await pool()
                    agent_task = asyncio.create_task(safe_auto_trigger(p))
                    last_progress = time.monotonic()

            await asyncio.sleep(settings.worker_idle_sleep_s)
