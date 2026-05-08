"""Воркер.

Единственная задача — стадия A: забирает скриншоты со статусом ocr_status='pending',
отдаёт vision-модели, пишет raw_ocr и переводит снимок в 'done' (или 'failed').

Стадия B живёт в HTTP-handler'е POST /api/chat и стартует только по сообщению
пользователя. Воркер о ней ничего не знает.
"""
import asyncio
import logging

import httpx

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
    log.info("worker start; ocr_concurrency=%d", n)

    running: set[asyncio.Task] = set()

    async with httpx.AsyncClient() as http:
        while True:
            free = n - len(running)
            if free > 0:
                async with (await pool()).acquire() as conn:
                    rows = await conn.fetch(_CLAIM_SQL, free)
                for r in rows:
                    t = asyncio.create_task(
                        _process(http, r["sha256"], r["bytes"], r["mime_type"])
                    )
                    running.add(t)
                    t.add_done_callback(running.discard)

            await asyncio.sleep(settings.worker_idle_sleep_s)
