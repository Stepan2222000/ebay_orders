"""Воркер.

Единственная задача — стадия A: забирает скриншоты со статусом ocr_status='pending',
отдаёт vision-модели, пишет raw_ocr и переводит снимок в 'done' (или 'failed').

Стадия B живёт в HTTP-handler'е POST /api/chat и стартует только по сообщению
пользователя. Воркер о ней ничего не знает.
"""
import asyncio
import logging

import asyncpg
from openai import AsyncOpenAI

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

# Лёгкая проверка наличия работы перед UPDATE-claim. SELECT не дёргает
# statement-level триггер screenshots_status_notify_upd, поэтому в простое воркер
# не плодит пустые NOTIFY status_changed (иначе SSE и фронт штормят запросами и
# душат загрузку). Покрыт частичным индексом screenshots_ocr_pending_idx.
_HAS_PENDING_SQL = "SELECT EXISTS(SELECT 1 FROM screenshots WHERE ocr_status = 'pending')"

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


async def _process(client: AsyncOpenAI, sha: bytes, image_bytes: bytes, mime: str) -> None:
    short = sha.hex()[:12]
    try:
        res = await transcribe(image_bytes, mime, client)
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
        "ocr ok   %s  lat=%.1fs  model=%s  is_order=%s  order_number=%s",
        short, res.latency_s, res.model,
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

    async with AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout_s,
    ) as client:
        while True:
            free = n - len(running)
            if free > 0:
                try:
                    async with (await pool()).acquire() as conn:
                        rows = (
                            await conn.fetch(_CLAIM_SQL, free)
                            if await conn.fetchval(_HAS_PENDING_SQL)
                            else []
                        )
                    for r in rows:
                        t = asyncio.create_task(
                            _process(client, r["sha256"], r["bytes"], r["mime_type"])
                        )
                        running.add(t)
                        t.add_done_callback(running.discard)
                except (
                    asyncpg.exceptions.ConnectionDoesNotExistError,
                    asyncpg.exceptions.InterfaceError,
                    asyncpg.exceptions.PostgresConnectionError,
                    ConnectionError,
                    OSError,
                ) as e:
                    log.warning("worker claim failed: %s; retrying", e)
                    await asyncio.sleep(2.0)
                    continue

            await asyncio.sleep(settings.worker_idle_sleep_s)
