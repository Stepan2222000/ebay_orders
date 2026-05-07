"""Воркер стадии A.

Цикл:
  1) забирает до N снимков из screenshots c FOR UPDATE SKIP LOCKED;
  2) для каждого параллельно вызывает OpenRouter;
  3) на успехе — пишет raw_ocr и помечает screenshots.ocr_status='done';
  4) на ошибке — помечает 'failed' c last_error.

Никаких авто-retries и второй модели — по SPEC.
"""
import asyncio
import json
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
INSERT INTO raw_ocr(sha256, model, raw_json) VALUES ($1, $2, $3::jsonb);
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
        log.error("fail %s: %s", short, e)
        return

    async with (await pool()).acquire() as conn:
        async with conn.transaction():
            await conn.execute(_INSERT_OCR_SQL, sha, res.model, json.dumps(res.raw_json))
            await conn.execute(_MARK_DONE_SQL, sha)
    obs = (res.raw_json.get("observed") or {})
    log.info(
        "ok   %s  lat=%.1fs  cost=%s  is_order=%s  order_number=%s",
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
    log.info("worker start; concurrency=%d", n)

    async with httpx.AsyncClient() as http:
        while True:
            async with (await pool()).acquire() as conn:
                rows = await conn.fetch(_CLAIM_SQL, n)
            if not rows:
                await asyncio.sleep(settings.worker_idle_sleep_s)
                continue
            log.info("claimed %d", len(rows))
            await asyncio.gather(*(
                _process(http, r["sha256"], r["bytes"], r["mime_type"])
                for r in rows
            ))
