"""Стриминговая обёртка вокруг OpenRouter chat completions.

stream_chat_step — async generator над одним LLM-шагом:
  - yield {"type":"text_delta","text":str}    — фрагмент текстового ответа
  - yield {"type":"step_finished",...}         — конец шага (есть tool_calls и/или текст)
  - yield {"type":"abort"}                     — abort_check вернул True
  - yield {"type":"error","error":str}         — HTTP/parse ошибка

Обёртка собирает дельты tool_calls и reasoning_details внутри себя — наружу отдаёт
готовые объекты в step_finished. Это сделано осознанно: для UI нам важен
прогрессивный текст, а tool-блоки рисуются по факту (pending → done) — частичные
args не нужны.

Между SSE-чанками вызывается abort_check; при True — stream.aclose() + abort event.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from .config import settings

log = logging.getLogger(__name__)


def build_chat_body(
    *,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    stream: bool = True,
) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "provider": {"ignore": list(settings.openrouter_ignore_providers)},
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    else:
        body["reasoning"] = {"enabled": False}
    if response_format is not None:
        body["response_format"] = response_format
    return body


def _merge_tool_call_delta(buf: dict[int, dict], delta: dict) -> None:
    """Накапливает дельты tool_calls по индексу — формат OpenRouter совпадает с OpenAI."""
    for tc in delta:
        idx = tc.get("index", 0)
        slot = buf.setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


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
    http: httpx.AsyncClient,
    body: dict,
    *,
    abort_check: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[dict]:
    """Один LLM-шаг через OpenRouter SSE. См. модульный docstring."""
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    tool_buf: dict[int, dict] = {}
    content_buf = ""
    reasoning_details: list[dict] = []
    finish_reason: str | None = None
    usage: dict | None = None
    provider: str | None = None

    try:
        async with http.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=settings.openrouter_timeout_s,
        ) as resp:
            if resp.status_code != 200:
                err_body = await resp.aread()
                yield {"type": "error", "error": f"HTTP {resp.status_code}: {err_body[:400]!r}"}
                return

            async for line in resp.aiter_lines():
                if abort_check is not None and await abort_check():
                    await resp.aclose()
                    yield {"type": "abort"}
                    return

                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if chunk.get("provider") and provider is None:
                    provider = chunk["provider"]
                if chunk.get("usage"):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch = choices[0]
                delta = ch.get("delta") or {}

                # текст
                if delta.get("content"):
                    txt = delta["content"]
                    content_buf += txt
                    yield {"type": "text_delta", "text": txt}

                # reasoning_details (для GPT-5.5: сохраняем и пробрасываем обратно)
                rd = delta.get("reasoning_details")
                if rd:
                    reasoning_details.extend(rd)

                # tool_calls (агрегируем)
                tcd = delta.get("tool_calls")
                if tcd:
                    _merge_tool_call_delta(tool_buf, tcd)

                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
    except httpx.HTTPError as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"}
        return

    yield {
        "type": "step_finished",
        "content": content_buf or None,
        "tool_calls": _finalize_tool_calls(tool_buf),
        "finish_reason": finish_reason or "stop",
        "usage": usage or {},
        "provider": provider,
        "reasoning_details": reasoning_details or None,
    }
