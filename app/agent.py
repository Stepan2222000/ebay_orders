"""Стадия B: агент-генератор.

Один публичный вызов: stream_stage_b(...). Async-генератор, который
yield-ит готовые UIMessageChunk-словари по протоколу v1
(start, start-step, text-*, tool-*, finish-step, finish).

Tool-loop поверх app.llm.stream_chat_step. Параллельные
tool-вызовы под Semaphore(40). chat completions stateless — reasoning
между шагами не пробрасывается (прокси этого не требует).

parts_acc заполняется по ходу — туда складывается ровно то, что
бэкенд позже сохранит в chat_messages.parts (text-парты + tool-плашки
с финальным state/output). Список мутируется in-place, чтобы внешний
finally в api.py видел частичный результат при cancel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

import asyncpg
from openai import AsyncOpenAI

from .config import settings
from .llm import build_chat_body, stream_chat_step
from .tools import OPENAI_TOOLS, execute_save_order, execute_sql, to_tool_content

log = logging.getLogger(__name__)


_TOOL_SEMAPHORE_SIZE = 40


async def _run_tool(name: str, args: dict, pool: asyncpg.Pool) -> dict:
    if name == "sql":
        return await execute_sql(pool, args.get("query") or "")
    if name == "save_order":
        return await execute_save_order(pool, args)
    return {"error": f"unknown tool: {name}"}


async def stream_stage_b(
    pool: asyncpg.Pool,
    client: AsyncOpenAI,
    messages_for_model: list[dict],
    message_id: str,
    parts_acc: list[dict],
) -> AsyncIterator[dict]:
    yield {"type": "start", "messageId": message_id}
    semaphore = asyncio.Semaphore(_TOOL_SEMAPHORE_SIZE)

    while True:
        yield {"type": "start-step"}

        body = build_chat_body(
            model=settings.agent_model,
            messages=messages_for_model,
            tools=OPENAI_TOOLS,
            max_tokens=settings.llm_max_tokens,
        )

        text_id: str | None = None
        live_text: dict | None = None
        finished: dict | None = None

        async for ev in stream_chat_step(client, body):
            t = ev["type"]
            if t == "text_delta":
                if text_id is None:
                    text_id = f"txt_{uuid.uuid4().hex[:8]}"
                    yield {"type": "text-start", "id": text_id}
                    live_text = {"type": "text", "text": ""}
                    parts_acc.append(live_text)
                live_text["text"] += ev["text"]
                yield {"type": "text-delta", "id": text_id, "delta": ev["text"]}
            elif t == "step_finished":
                finished = ev
            elif t == "error":
                if text_id is not None:
                    yield {"type": "text-end", "id": text_id}
                # Ошибку LLM показываем как видимое сообщение ассистента и
                # кладём в parts_acc — иначе она теряется и при reload, и в UI
                # (useChat.error в ChatPane не рендерится). Никаких тихих фолбеков.
                err_text = f"⚠️ Ошибка LLM: {ev['error']}"
                err_id = f"txt_{uuid.uuid4().hex[:8]}"
                yield {"type": "text-start", "id": err_id}
                yield {"type": "text-delta", "id": err_id, "delta": err_text}
                yield {"type": "text-end", "id": err_id}
                parts_acc.append({"type": "text", "text": err_text})
                yield {"type": "finish-step"}
                yield {"type": "finish"}
                return

        text_buf = live_text["text"] if live_text else ""
        if text_id is not None:
            yield {"type": "text-end", "id": text_id}

        tool_calls = (finished or {}).get("tool_calls") or []
        if not tool_calls:
            yield {"type": "finish-step"}
            yield {"type": "finish"}
            return

        # assistant с tool_calls для следующего шага
        assistant_msg: dict = {
            "role": "assistant",
            "content": text_buf or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ],
        }
        messages_for_model.append(assistant_msg)

        # tool-input-* в порядке модели; держим прямые ссылки на плашки
        # для O(1) обновления state/output по мере готовности.
        plate_by_id: dict[str, dict] = {}
        for tc in tool_calls:
            yield {
                "type": "tool-input-start",
                "toolCallId": tc["id"],
                "toolName": tc["name"],
            }
            yield {
                "type": "tool-input-available",
                "toolCallId": tc["id"],
                "toolName": tc["name"],
                "input": tc["arguments"],
            }
            plate = {
                "type": f"tool-{tc['name']}",
                "toolCallId": tc["id"],
                "state": "input-available",
                "input": tc["arguments"],
            }
            parts_acc.append(plate)
            plate_by_id[tc["id"]] = plate

        async def run_one(tc: dict):
            async with semaphore:
                result = await _run_tool(tc["name"], tc["arguments"], pool)
                return tc, result

        tasks = [asyncio.create_task(run_one(tc)) for tc in tool_calls]

        for fut in asyncio.as_completed(tasks):
            tc, result = await fut
            errored = isinstance(result, dict) and "error" in result
            yield {
                "type": "tool-output-available",
                "toolCallId": tc["id"],
                "output": result,
            }
            plate = plate_by_id[tc["id"]]
            plate["state"] = "output-error" if errored else "output-available"
            plate["output"] = result
            messages_for_model.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": to_tool_content(result),
            })

        yield {"type": "finish-step"}
