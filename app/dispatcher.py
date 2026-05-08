"""Диспетчер агентской сессии.

Запускается воркером по триггеру (новые pending снимки или новое user-сообщение).
Создаёт agent_run + assistant chat_message, прокручивает run_agent_session,
по мере событий пишет parts в БД (UPDATE chat_messages.parts).

Один диспетчер — одна сессия. Воркер гарантирует, что в каждый момент времени
крутится максимум одна агентская задача.

asyncpg в этом проекте сериализует jsonb через json.dumps как codec
(см. app/db.py:_init_conn), поэтому в SQL передаём list/dict напрямую.
"""
import logging
import time
from typing import Any

import asyncpg
import httpx

from .agent import load_pending_snapshots, run_agent_session

log = logging.getLogger(__name__)


_INSERT_AGENT_RUN_SQL = """
INSERT INTO agent_run(message_id) VALUES ($1) RETURNING run_id;
"""

_INSERT_ASSISTANT_SQL = """
INSERT INTO chat_messages(session_id, role, parts)
VALUES ('default', 'assistant', $1::jsonb)
RETURNING message_id;
"""

_UPDATE_PARTS_SQL = """
UPDATE chat_messages SET parts=$1::jsonb WHERE message_id=$2;
"""

_UPDATE_AGENT_RUN_MSGID_SQL = """
UPDATE agent_run SET message_id=$2 WHERE run_id=$1;
"""

_FINISH_AGENT_RUN_SQL = """
UPDATE agent_run SET finished_at=now(), thinking=false WHERE run_id=$1;
"""

_FLUSH_THROTTLE_S = 0.25


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    nn = abs(n) % 100
    if 11 <= nn <= 14:
        return forms[2]
    nn %= 10
    if nn == 1:
        return forms[0]
    if 2 <= nn <= 4:
        return forms[1]
    return forms[2]


class _PartsBuffer:
    """Хранит parts в памяти + флушит в БД с троттлингом."""

    def __init__(self, pool: asyncpg.Pool, message_id: int):
        self.pool = pool
        self.message_id = message_id
        self.parts: list[dict] = []
        self._last_flush = 0.0
        self._dirty = False

    def append_text(self, text: str) -> None:
        if self.parts and self.parts[-1].get("type") == "text":
            self.parts[-1]["text"] = self.parts[-1].get("text", "") + text
        else:
            self.parts.append({"type": "text", "text": text})
        self._dirty = True

    def append_tool_start(self, tool_id: str, name: str, args: Any) -> None:
        self.parts.append({
            "type": f"tool-{name}",
            "toolCallId": tool_id,
            "state": "input-available",
            "input": args,
        })
        self._dirty = True

    def update_tool_done(self, tool_id: str, name: str, result: Any) -> None:
        for p in self.parts:
            if p.get("toolCallId") == tool_id and p.get("type") == f"tool-{name}":
                if isinstance(result, dict) and "error" in result:
                    p["state"] = "output-error"
                    p["errorText"] = str(result["error"])
                    p["output"] = result
                else:
                    p["state"] = "output-available"
                    p["output"] = result
                self._dirty = True
                return
        # fallback: парт не нашёлся (странно), допишем как готовый
        self.parts.append({
            "type": f"tool-{name}",
            "toolCallId": tool_id,
            "state": "output-error" if isinstance(result, dict) and "error" in result
                     else "output-available",
            "output": result,
        })
        self._dirty = True

    async def flush(self, *, force: bool = False) -> None:
        if not self._dirty:
            return
        now = time.monotonic()
        if not force and (now - self._last_flush) < _FLUSH_THROTTLE_S:
            return
        await self.pool.execute(_UPDATE_PARTS_SQL, self.parts, self.message_id)
        self._last_flush = now
        self._dirty = False


async def run_dispatched_session(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    *,
    auto_nudge: bool,
) -> None:
    """Создаёт agent_run + assistant message, гонит сессию, пишет parts по ходу."""
    # 1. INSERT agent_run + assistant chat_message
    run_id = await pool.fetchval(_INSERT_AGENT_RUN_SQL, None)
    initial_parts: list[dict] = []
    if auto_nudge:
        # Стартовое сообщение — сразу видно «агент проснулся».
        pending = await load_pending_snapshots(pool)
        n = len(pending)
        if n:
            word = _ru_plural(n, ("снимок", "снимка", "снимков"))
            initial_parts = [{
                "type": "text",
                "text": f"Вижу {n} {word} в очереди — собираю заказы.",
            }]
    message_id = await pool.fetchval(_INSERT_ASSISTANT_SQL, initial_parts)
    await pool.execute(_UPDATE_AGENT_RUN_MSGID_SQL, run_id, message_id)
    log.info("dispatcher: run_id=%s message_id=%s auto_nudge=%s", run_id, message_id, auto_nudge)

    buf = _PartsBuffer(pool, message_id)
    buf.parts = list(initial_parts)

    aborted = False
    try:
        async for ev in run_agent_session(pool, http, run_id, auto_nudge=auto_nudge):
            t = ev["type"]
            if t == "text_delta":
                buf.append_text(ev["text"])
                await buf.flush()
            elif t == "tool_start":
                buf.append_tool_start(ev["id"], ev["name"], ev["arguments"])
                await buf.flush(force=True)
            elif t == "tool_done":
                buf.update_tool_done(ev["id"], ev["name"], ev["result"])
                await buf.flush(force=True)
            elif t == "abort":
                aborted = True
                buf.append_text("\n\n*(прервано пользователем)*")
                break
            elif t == "finish":
                break
    except Exception as e:
        log.exception("dispatcher: agent loop crashed: %s", e)
        buf.append_text(f"\n\n*(внутренняя ошибка: {type(e).__name__})*")
    finally:
        await buf.flush(force=True)
        await pool.execute(_FINISH_AGENT_RUN_SQL, run_id)
        log.info("dispatcher: run_id=%s finished aborted=%s", run_id, aborted)
