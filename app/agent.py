"""Стадия B — агентский цикл.

run_agent_session(...) — общий вход.
- Кладёт в LLM системный промпт (с pending снимками) + историю чата + новое user-сообщение.
- Гоняет цикл tool-calling до finish_reason='stop' (без жёсткого лимита).
- Возвращает массив parts ассистента (text + tool блоки) для записи в chat_messages.
"""
import base64
import json
import logging
from typing import Any

import asyncpg
import httpx

from .config import settings
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
    return [{"sha": r["sha256"].hex(), "observed": r["observed"]} for r in rows]


def _history_to_messages(rows) -> list[dict]:
    """Сжимаем persisted parts в обычные text-сообщения для LLM.

    Прикреплённые файлы из истории не пересылаем повторно (дорого);
    tool-блоки сворачиваем в короткую сводку.
    """
    out = []
    for r in rows:
        chunks: list[str] = []
        for p in r["parts"]:
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


# ─── вызов LLM ──────────────────────────────────────────────────────────────

async def _call_llm(http: httpx.AsyncClient, messages: list[dict]) -> dict:
    body = {
        "model": settings.openrouter_model,
        "max_tokens": settings.openrouter_max_tokens,
        "reasoning": {"enabled": False},
        "provider": {"ignore": list(settings.openrouter_ignore_providers)},
        "tools": OPENAI_TOOLS,
        "tool_choice": "auto",
        "messages": messages,
    }
    r = await http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        timeout=settings.openrouter_timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


# ─── собственно цикл ───────────────────────────────────────────────────────

def _build_user_content(text: str | None, files: list[tuple[bytes, str]] | None) -> Any:
    """Если есть файлы — multimodal-content; иначе чистая строка."""
    if not files:
        return text
    blocks = [{"type": "text", "text": text}] if text else []
    for data, mime in files:
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return blocks


async def run_agent_session(
    pool: asyncpg.Pool,
    http: httpx.AsyncClient,
    *,
    user_text: str | None = None,
    user_files: list[tuple[bytes, str]] | None = None,
    auto_nudge: bool = False,
) -> list[dict]:
    """Returns parts ассистента для записи в chat_messages."""
    pending = await load_pending_snapshots(pool)
    system_prompt = build_stage_b_system(pending)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(await load_chat_history(pool))

    if auto_nudge:
        messages.append({
            "role": "user",
            "content": (
                "Появились новые распознанные снимки в очереди стадии B. "
                "Сгруппируй их в заказы по сильным признакам и сохрани через save_order. "
                "Если какой-то снимок нельзя строго связать с заказом — пометь его как "
                "failed через sql (UPDATE screenshots SET agent_status='failed', "
                "last_error='...' WHERE sha256=decode('<hex>','hex'))."
            ),
        })
    elif user_text or user_files:
        messages.append({
            "role": "user",
            "content": _build_user_content(user_text, user_files),
        })

    parts: list[dict] = []
    step = 0
    while True:
        step += 1
        try:
            resp = await _call_llm(http, messages)
        except Exception as e:
            parts.append({"type": "text", "text": f"Не получилось обратиться к модели: {e}"})
            break

        choice = resp["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason")
        usage = resp.get("usage") or {}
        log.info("agent step=%d finish=%s cost=$%s provider=%s",
                 step, finish, usage.get("cost"), resp.get("provider"))

        if msg.get("content"):
            parts.append({"type": "text", "text": msg["content"]})

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break

        messages.append({
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": msg["tool_calls"],
        })

        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                parts.append({"type": "tool", "name": name,
                              "arguments": raw_args,
                              "result": {"error": f"invalid tool arguments: {e}"}})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": name,
                                 "content": to_tool_content({"error": "invalid arguments"})})
                continue

            if name == "sql":
                result = await execute_sql(pool, args.get("query", ""))
            elif name == "save_order":
                result = await execute_save_order(pool, args)
            else:
                result = {"error": f"неизвестный инструмент: {name}"}

            parts.append({"type": "tool", "name": name, "arguments": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": name,
                "content": to_tool_content(result),
            })

        if finish == "stop":
            break

    return parts


# ─── авто-trigger для стадии B (вызывается воркером в idle) ───────────────

_TRIGGER_LOCK_KEY = 0xEB0102

_INSERT_AUTO_MSG_SQL = """
INSERT INTO chat_messages(session_id, role, parts)
VALUES ('default', 'assistant', $1);
"""


async def maybe_run_auto_trigger(pool: asyncpg.Pool, http: httpx.AsyncClient) -> bool:
    """Если есть pending снимки для B и нам удалось взять advisory-lock — запускаем агента.
    Возвращает True если агент действительно прогонялся.
    """
    n = await pool.fetchval(
        "SELECT count(*) FROM screenshots "
        "WHERE ocr_status='done' AND agent_status='pending' AND order_id IS NULL"
    )
    if not n:
        return False
    async with pool.acquire() as conn:
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _TRIGGER_LOCK_KEY)
        if not got:
            return False
        try:
            log.info("auto-trigger: pending=%d", n)
            parts = await run_agent_session(pool, http, auto_nudge=True)
            await conn.execute(_INSERT_AUTO_MSG_SQL, parts)
            return True
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _TRIGGER_LOCK_KEY)
