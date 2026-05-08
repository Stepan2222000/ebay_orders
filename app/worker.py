"""Воркер.

Обязанности:
  1) стадия A — забирает pending screenshots, отдаёт vision-модели, пишет raw_ocr;
  2) стадия B — единственный диспетчер агентских сессий. Запускает run_dispatched_session
     по двум триггерам:
        - есть user-сообщение в чате без assistant-ответа (NOTIFY user_message_arrived);
        - стадия A простаивала ≥ AUTO_TRIGGER_IDLE_S и накопились pending screenshots
          для сборки заказов.

В каждый момент времени активна максимум одна агентская сессия — гарантируется
agent_task != None / not done. Cross-process защита тоже есть: триггер только
один (этот воркер).
"""
import asyncio
import logging
import time

import asyncpg
import httpx

from .config import settings
from .db import pool
from .dispatcher import run_dispatched_session
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

_PENDING_AGENT_COUNT_SQL = """
SELECT count(*) FROM screenshots
 WHERE ocr_status='done' AND agent_status='pending' AND order_id IS NULL;
"""

# Есть user-сообщение без assistant-ответа?
_USER_NEEDS_REPLY_SQL = """
SELECT EXISTS (
    SELECT 1 FROM chat_messages u
     WHERE u.session_id='default' AND u.role='user'
       AND NOT EXISTS (
           SELECT 1 FROM chat_messages a
            WHERE a.session_id='default' AND a.role='assistant'
              AND a.created_at > u.created_at
       )
);
"""

_CLOSE_STALE_RUNS_SQL = """
UPDATE agent_run SET finished_at=now(), thinking=false
 WHERE finished_at IS NULL;
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


# ─── listener: NOTIFY user_message_arrived → флаг ────────────────────────────

async def _listen_user_messages(stop: asyncio.Event, flag: asyncio.Event) -> None:
    """Держит выделенный коннект, перепоставляет listener при дисконнекте."""
    while not stop.is_set():
        try:
            conn = await asyncpg.connect(
                host=settings.pg_host, port=settings.pg_port,
                user=settings.pg_user, password=settings.pg_password,
                database=settings.pg_database,
            )
        except Exception as e:
            log.error("listener connect failed: %s", e)
            await asyncio.sleep(2)
            continue

        loop = asyncio.get_running_loop()

        def _cb(_c, _pid, _channel, _payload):
            loop.call_soon_threadsafe(flag.set)

        try:
            await conn.add_listener("user_message_arrived", _cb)
            log.info("listening on 'user_message_arrived'")
            while not stop.is_set():
                await asyncio.sleep(30)
                # liveness check; падение здесь приведёт к reconnect через outer try
                await conn.execute("SELECT 1")
        except Exception as e:
            log.warning("listener loop dropped: %s", e)
        finally:
            try:
                await conn.close()
            except Exception:
                pass


# ─── главный цикл ────────────────────────────────────────────────────────────

async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    n = settings.worker_concurrency
    log.info("worker start; ocr_concurrency=%d, auto_trigger_idle=%.1fs",
             n, _AUTO_TRIGGER_IDLE_S)

    # Закрываем висячие сессии после крэша.
    closed = await (await pool()).execute(_CLOSE_STALE_RUNS_SQL)
    if closed and closed != "UPDATE 0":
        log.info("closed stale agent_run sessions: %s", closed)

    running: set[asyncio.Task] = set()
    agent_task: asyncio.Task | None = None
    last_progress = time.monotonic()
    stop_evt = asyncio.Event()
    user_msg_flag = asyncio.Event()
    listener = asyncio.create_task(_listen_user_messages(stop_evt, user_msg_flag))

    async def safe_dispatch(p, *, auto_nudge: bool):
        try:
            await run_dispatched_session(p, http, auto_nudge=auto_nudge)
        except Exception as e:
            log.exception("dispatcher failed: %s", e)

    try:
        async with httpx.AsyncClient() as http:
            while True:
                # ─── стадия A ───────────────────────────────────────────────
                free = n - len(running)
                rows: list = []
                if free > 0:
                    async with (await pool()).acquire() as conn:
                        rows = await conn.fetch(_CLAIM_SQL, free)
                    for r in rows:
                        t = asyncio.create_task(
                            _process(http, r["sha256"], r["bytes"], r["mime_type"])
                        )
                        running.add(t)
                        t.add_done_callback(running.discard)

                if rows or running:
                    last_progress = time.monotonic()

                # ─── стадия B (единый диспетчер) ────────────────────────────
                if agent_task is None or agent_task.done():
                    p = await pool()

                    # триггер 1: есть user-сообщение без ответа
                    if user_msg_flag.is_set():
                        user_msg_flag.clear()
                        needs = await p.fetchval(_USER_NEEDS_REPLY_SQL)
                        if needs:
                            agent_task = asyncio.create_task(
                                safe_dispatch(p, auto_nudge=False)
                            )
                            last_progress = time.monotonic()
                            await asyncio.sleep(settings.worker_idle_sleep_s)
                            continue

                    # триггер 2: idle + есть pending снимки для агента
                    idle = time.monotonic() - last_progress
                    if idle >= _AUTO_TRIGGER_IDLE_S and not running and not rows:
                        pending_n = await p.fetchval(_PENDING_AGENT_COUNT_SQL)
                        if pending_n:
                            agent_task = asyncio.create_task(
                                safe_dispatch(p, auto_nudge=True)
                            )
                            last_progress = time.monotonic()

                await asyncio.sleep(settings.worker_idle_sleep_s)
    finally:
        stop_evt.set()
        listener.cancel()
