"""Стадия B — агентский цикл (streaming + abort-aware).

run_agent_session(pool, http, run_id, *, auto_nudge) — async generator,
выдаёт события по мере работы:
  - {"type":"text_delta","text":str}                       — фрагмент текста модели
  - {"type":"tool_start","id","name","arguments"}          — старт вызова инструмента
  - {"type":"tool_done","id","name","arguments","result"}  — результат
  - {"type":"abort"}                                       — пользователь нажал Stop
  - {"type":"finish","reason"}                             — конец сессии

Использует stream_chat_step из openrouter.py (httpx streaming + tool_call deltas
+ reasoning_details для GPT-5.5). Между чанками проверяет agent_run.stop_requested
с кешем 0.5s. Перед/после каждого LLM-вызова обновляет agent_run.thinking.
"""
import json
import logging
import time
from typing import AsyncIterator

import asyncpg
import httpx

from .config import settings
from .openrouter import build_chat_body, stream_chat_step
from .prompt import build_stage_b_system
from .tools import (
    OPENAI_TOOLS,
    execute_save_order,
    execute_sql,
    to_tool_content,
)

log = logging.getLogger(__name__)


# ─── загрузка контекста ─────────────────────────────────────────────────────

_PENDING_SQL = """
SELECT s.sha256, r.raw_json->'observed' AS observed
  FROM screenshots s
  JOIN raw_ocr r USING (sha256)
 WHERE s.ocr_status = 'done'
   AND s.agent_status = 'pending'
   AND s.order_id IS NULL
 ORDER BY s.created_at;
"""

_HISTORY_SQL = """
SELECT role, parts
  FROM chat_messages
 WHERE session_id = 'default'
 ORDER BY created_at, message_id;
"""


async def load_pending_snapshots(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(_PENDING_SQL)
    out: list[dict] = []
    for r in rows:
        obs = r["observed"]
        if isinstance(obs, str):
            try:
                obs = json.loads(obs)
            except json.JSONDecodeError:
                obs = {}
        out.append({"sha": r["sha256"].hex(), "observed": obs or {}})
    return out


def _history_to_messages(rows) -> list[dict]:
    """Сжимаем persisted parts в обычные text-сообщения для LLM."""
    out = []
    for r in rows:
        parts = r["parts"]
        if isinstance(parts, str):
            try:
                parts = json.loads(parts)
            except json.JSONDecodeError:
                parts = []
        chunks: list[str] = []
        for p in parts or []:
            t = p.get("type")
            if t == "text":
                chunks.append(p.get("text", ""))
            elif t == "file":
                chunks.append("[фото в сообщении]")
            elif t == "tool":
                err = (p.get("result") or {}).get("error")
                if err:
                    chunks.append(f"[{p.get('name')}: ошибка — {err}]")
                else:
                    chunks.append(f"[{p.get('name')}: выполнено]")
        text = "\n".join(c for c in chunks if c)
        if text:
            out.append({"role": r["role"], "content": text})
    return out


async def load_chat_history(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch(_HISTORY_SQL)
    return _history_to_messages(rows)


# ─── stop check (с кешем 0.5s) ──────────────────────────────────────────────

def _make_abort_check(pool: asyncpg.Pool, run_id: int):
    state = {"last_check": 0.0, "value": False}

    async def check() -> bool:
        now = time.monotonic()
        if now - state["last_check"] > 0.5:
            state["value"] = bool(await pool.fetchval(
                "SELECT stop_requested FROM agent_run WHERE run_id=$1", run_id
            ))
            state["last_check"] = now
        return state["value"]

    return check


# ─── собственно цикл ───────────────────────────────────────────────────────

_AUTO_NUDGE = (
    "Появились новые распознанные снимки в очереди стадии B. "
    "Сгруппируй их в заказы по сильным признакам и сохрани через save_order. "
    "Если какой-то снимок нельзя строго связать с заказом — пометь его как "
    "failed через sql (UPDATE screenshots SET agent_status='failed', "
    "last_error='...' WHERE sha256=decode('<hex>','hex'))."
)


async def run_agent_session(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    run_id: int,
    *,
    auto_nudge: bool = False,
) -> AsyncIterator[dict]:
    """Async generator: событие за событием. См. модульный docstring."""
    pending = await load_pending_snapshots(pool)
    system_prompt = build_stage_b_system(pending)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(await load_chat_history(pool))

    if auto_nudge:
        messages.append({"role": "user", "content": _AUTO_NUDGE})

    abort_check = _make_abort_check(pool, run_id)

    step = 0
    while True:
        step += 1
        await pool.execute(
            "UPDATE agent_run SET thinking=true WHERE run_id=$1", run_id
        )
        body = build_chat_body(
            model=settings.openrouter_model_b,
            messages=messages,
            tools=OPENAI_TOOLS,
            reasoning_effort=settings.openrouter_reasoning_effort_b,
            max_tokens=settings.openrouter_max_tokens,
            stream=True,
        )

        final = None
        try:
            async for ev in stream_chat_step(http, body, abort_check=abort_check):
                if ev["type"] == "text_delta":
                    yield {"type": "text_delta", "text": ev["text"]}
                elif ev["type"] == "abort":
                    yield {"type": "abort"}
                    await pool.execute(
                        "UPDATE agent_run SET thinking=false WHERE run_id=$1", run_id
                    )
                    return
                elif ev["type"] == "error":
                    yield {"type": "text_delta",
                           "text": f"\nНе получилось обратиться к модели: {ev['error']}"}
                    yield {"type": "finish", "reason": "error"}
                    await pool.execute(
                        "UPDATE agent_run SET thinking=false WHERE run_id=$1", run_id
                    )
                    return
                elif ev["type"] == "step_finished":
                    final = ev
        finally:
            await pool.execute(
                "UPDATE agent_run SET thinking=false WHERE run_id=$1", run_id
            )

        if final is None:
            yield {"type": "finish", "reason": "error"}
            return

        log.info(
            "agent step=%d finish=%s tools=%d cost=$%s provider=%s",
            step, final["finish_reason"], len(final["tool_calls"]),
            (final["usage"] or {}).get("cost"), final.get("provider"),
        )

        # Закрепляем assistant-сообщение в истории для следующих шагов.
        # Для GPT-5.5 кладём reasoning_details обратно — иначе модель «теряет мысль»
        # между tool-loops (требование OpenAI Responses API; OpenRouter поддерживает
        # тот же протокол через chat/completions).
        assistant_msg: dict = {
            "role": "assistant",
            "content": final["content"],
        }
        if final["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                    },
                }
                for tc in final["tool_calls"]
            ]
        if final.get("reasoning_details"):
            assistant_msg["reasoning_details"] = final["reasoning_details"]
        messages.append(assistant_msg)

        if not final["tool_calls"]:
            yield {"type": "finish", "reason": final["finish_reason"]}
            return

        for tc in final["tool_calls"]:
            tc_id = tc["id"]
            name = tc["name"]
            args = tc["arguments"]
            yield {"type": "tool_start", "id": tc_id, "name": name, "arguments": args}

            if isinstance(args, dict) and args.get("__error__"):
                result = {"error": f"invalid tool arguments: {args.get('__error__')}"}
            elif name == "sql":
                result = await execute_sql(pool, (args or {}).get("query", ""))
            elif name == "save_order":
                result = await execute_save_order(pool, args or {})
            else:
                result = {"error": f"неизвестный инструмент: {name}"}

            yield {"type": "tool_done", "id": tc_id, "name": name,
                   "arguments": args, "result": result}
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": to_tool_content(result),
            })

            if await abort_check():
                yield {"type": "abort"}
                return

        if final["finish_reason"] == "stop":
            # Модель сказала «stop», но при этом tool_calls — формально это
            # сигнал «я закончила вызовы и больше ничего не скажу».
            yield {"type": "finish", "reason": "stop"}
            return
