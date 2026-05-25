"""Стриминговая обёртка вокруг chat completions (OpenAI SDK → gpt55-прокси).

stream_chat_step — async generator над одним LLM-шагом:
  - yield {"type":"text_delta","text":str}    — фрагмент текстового ответа
  - yield {"type":"step_finished",...}         — конец шага (tool_calls и/или текст)
  - yield {"type":"abort"}                     — abort_check вернул True
  - yield {"type":"error","error":str}         — ошибка API

Дельты tool_calls собираются внутри по index — наружу отдаются готовые объекты
в step_finished. reasoning между шагами НЕ пробрасывается: chat completions
stateless, прокси этого не требует (effort задан суффиксом имени модели).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from openai import APIError, AsyncOpenAI

log = logging.getLogger(__name__)


def build_chat_body(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    stream: bool = True,
) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if response_format is not None:
        body["response_format"] = response_format
    return body


def _merge_tool_call_delta(buf: dict[int, dict], tool_calls) -> None:
    """Накапливает дельты tool_calls по индексу (объекты SDK ChoiceDeltaToolCall)."""
    for tc in tool_calls:
        idx = tc.index or 0
        slot = buf.setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if tc.id:
            slot["id"] = tc.id
        fn = tc.function
        if fn and fn.name:
            slot["name"] = fn.name
        if fn and fn.arguments:
            slot["arguments"] += fn.arguments


def _finalize_tool_calls(buf: dict[int, dict]) -> list[dict]:
    """Собирает tool_calls в порядке индексов; парсит arguments в dict."""
    out: list[dict] = []
    for idx in sorted(buf):
        slot = buf[idx]
        if not slot["name"] or slot["id"] is None:
            continue
        try:
            args = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {"__raw__": slot["arguments"], "__error__": "invalid json"}
        out.append({"id": slot["id"], "name": slot["name"], "arguments": args})
    return out


async def stream_chat_step(
    client: AsyncOpenAI,
    body: dict,
    *,
    abort_check: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[dict]:
    """Один LLM-шаг через chat.completions stream. См. модульный docstring."""
    tool_buf: dict[int, dict] = {}
    content_buf = ""
    finish_reason: str | None = None

    try:
        stream = await client.chat.completions.create(**body)
    except APIError as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}"}
        return

    try:
        async for chunk in stream:
            if abort_check is not None and await abort_check():
                await stream.close()
                yield {"type": "abort"}
                return

            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            delta = ch.delta

            if delta and delta.content:
                content_buf += delta.content
                yield {"type": "text_delta", "text": delta.content}

            if delta and delta.tool_calls:
                _merge_tool_call_delta(tool_buf, delta.tool_calls)

            if ch.finish_reason:
                finish_reason = ch.finish_reason
    except APIError as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}"}
        return

    yield {
        "type": "step_finished",
        "content": content_buf or None,
        "tool_calls": _finalize_tool_calls(tool_buf),
        "finish_reason": finish_reason or "stop",
    }
