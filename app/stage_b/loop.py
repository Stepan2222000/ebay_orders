"""Stage B agent loop with safety nets.

Yields raw SSE chunks (bytes) so /api/chat can pass them straight to a
StreamingResponse. Returns the final assistant text so the handler can
persist it to chat_messages before [DONE].
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator, Literal

import asyncpg
from openai import AsyncOpenAI

from app.config import settings
from app.stage_b import dispatch, stream as ss
from app.stage_b.tools import tools_for

log = logging.getLogger("stage_b.loop")

_MODEL = "moonshotai/kimi-k2.6"
_MAX_STEPS = 8
_TIMEOUTS = {
    "sql_read": 8.0,
    "save_order_details": 12.0,
    "delete_order": 4.0,
    "no_consolidation": 1.0,
}


def _normalise_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())


async def _run_tool(
    name: str,
    args: dict[str, Any],
    *,
    pool: asyncpg.Pool,
    last_user_text: str,
) -> str:
    if name == "sql_read":
        return await dispatch.run_sql_read(pool, args)
    if name == "save_order_details":
        return await dispatch.run_save_order_details(pool, args)
    if name == "delete_order":
        return await dispatch.run_delete_order(pool, args, last_user_text=last_user_text)
    if name == "no_consolidation":
        return json.dumps({"ok": True})
    return json.dumps({"error": f"unknown tool {name}"})


async def run(
    messages: list[dict[str, Any]],
    *,
    branch: Literal["screenshot", "text"],
    pool: asyncpg.Pool,
    last_user_text: str,
    progress: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Drive the agent and stream UI Message chunks. Returns nothing —
    the final assistant text is persisted by the caller via the
    last_text closure-style sentinel below."""

    client = AsyncOpenAI(
        api_key=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        timeout=settings.STAGE_A_TIMEOUT_SECONDS,
        max_retries=0,
    )
    progress = progress or {}
    progress_id = "progress-1"

    seen_queries: set[str] = set()
    final_text_chunks: list[str] = []

    try:
        message_id = f"msg-{uuid.uuid4().hex}"
        yield ss.chunk_start(message_id)
        yield ss.chunk_start_step()
        if progress:
            yield ss.chunk_data(progress_id, "progress", progress)

        for step in range(_MAX_STEPS):
            resp = await client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                tools=tools_for(branch),
                tool_choice="auto",
                temperature=0,
                max_tokens=4000,
                extra_body={
                    "reasoning": {"enabled": False},
                    "provider": {"ignore": ["Inceptron"]},
                },
            )
            m = resp.choices[0].message
            if not m.tool_calls:
                text = m.content or ""
                final_text_chunks.append(text)
                text_id = f"text-{uuid.uuid4().hex}"
                yield ss.chunk_text_start(text_id)
                if text:
                    yield ss.chunk_text_delta(text_id, text)
                yield ss.chunk_text_end(text_id)
                break

            messages.append(m.model_dump(exclude_none=True))
            for tc in m.tool_calls:
                name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except Exception as e:  # noqa: BLE001
                    result = json.dumps({"error": f"bad args json: {e}"})
                else:
                    if name == "sql_read":
                        key = _normalise_query(args.get("query", ""))
                        if key in seen_queries:
                            result = json.dumps(
                                {"error": "duplicate query, you already asked this; "
                                          "proceed to the next step"}
                            )
                        else:
                            seen_queries.add(key)
                            try:
                                result = await asyncio.shield(
                                    asyncio.wait_for(
                                        _run_tool(
                                            name, args,
                                            pool=pool,
                                            last_user_text=last_user_text,
                                        ),
                                        timeout=_TIMEOUTS[name],
                                    )
                                )
                            except asyncio.TimeoutError:
                                result = json.dumps(
                                    {"error": f"{name} timed out after {_TIMEOUTS[name]}s"}
                                )
                            except Exception as e:  # noqa: BLE001
                                log.exception("sql_read crashed")
                                result = json.dumps(
                                    {"error": f"{type(e).__name__}: {e}"}
                                )
                    else:
                        try:
                            result = await asyncio.shield(
                                asyncio.wait_for(
                                    _run_tool(
                                        name, args,
                                        pool=pool,
                                        last_user_text=last_user_text,
                                    ),
                                    timeout=_TIMEOUTS.get(name, 12.0),
                                )
                            )
                        except asyncio.TimeoutError:
                            result = json.dumps(
                                {"error": f"{name} timed out after {_TIMEOUTS.get(name, 12.0)}s"}
                            )
                        except Exception as e:  # noqa: BLE001
                            log.exception("%s crashed", name)
                            result = json.dumps(
                                {"error": f"{type(e).__name__}: {e}"}
                            )

                # progress hint per tool call
                progress.setdefault("tool_calls", 0)
                progress["tool_calls"] += 1
                progress["last_tool"] = name
                yield ss.chunk_data(progress_id, "progress", progress)

                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )

        else:
            # max_steps reached — tell user we gave up.
            text = (
                "Не уверена в результате — превышен лимит шагов. "
                "Попробуй прислать снимки меньшей пачкой или сформулировать "
                "запрос точнее."
            )
            final_text_chunks.append(text)
            text_id = f"text-{uuid.uuid4().hex}"
            yield ss.chunk_text_start(text_id)
            yield ss.chunk_text_delta(text_id, text)
            yield ss.chunk_text_end(text_id)

        yield ss.chunk_finish_step()
        yield ss.chunk_finish()
        yield ss.chunk_done()
    finally:
        await client.close()

    # Surface the final text to the caller through the messages list
    # (last entry is implied from final_text_chunks).
    # We piggy-back on the messages container so /api/chat can find it.
    messages.append({"role": "_final_text", "content": "".join(final_text_chunks)})
