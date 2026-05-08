"""Кодировщик внутренних событий агента в Data Stream Protocol AI SDK v6.

Формат — Server-Sent Events с типизированными JSON-объектами. Контракт:
https://v6.ai-sdk.dev/docs/ai-sdk-ui/stream-protocol

Заголовок ответа: `x-vercel-ai-ui-message-stream: v1`.
Терминатор стрима: `data: [DONE]\\n\\n`.
"""
import json
import uuid
from typing import AsyncIterator


SSE_HEADERS: dict[str, str] = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
    "x-vercel-ai-ui-message-stream": "v1",
}


def _sse(obj) -> bytes:
    if obj == "[DONE]":
        return b"data: [DONE]\n\n"
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


async def encode_ui_message_stream(
    events: AsyncIterator[dict],
    message_id: str | None = None,
) -> AsyncIterator[bytes]:
    """Пропускает поток событий агента наружу как UIMessageStream v1.

    Внутренние события агента:
        {"type":"text", "text": ...}
        {"type":"tool_start", "id": ..., "name": ..., "arguments": ...}
        {"type":"tool_done",  "id": ..., "name": ..., "arguments": ..., "result": ...}
        {"type":"finish", "reason": ...}
    """
    if message_id is None:
        message_id = f"msg_{uuid.uuid4().hex}"

    yield _sse({"type": "start", "messageId": message_id})
    yield _sse({"type": "start-step"})

    try:
        async for ev in events:
            t = ev["type"]
            if t == "text":
                tid = f"text_{uuid.uuid4().hex[:8]}"
                yield _sse({"type": "text-start", "id": tid})
                yield _sse({"type": "text-delta", "id": tid, "delta": ev["text"]})
                yield _sse({"type": "text-end", "id": tid})
            elif t == "tool_start":
                yield _sse({
                    "type": "tool-input-start",
                    "toolCallId": ev["id"],
                    "toolName": ev["name"],
                })
                yield _sse({
                    "type": "tool-input-available",
                    "toolCallId": ev["id"],
                    "toolName": ev["name"],
                    "input": ev["arguments"],
                })
            elif t == "tool_done":
                yield _sse({
                    "type": "tool-output-available",
                    "toolCallId": ev["id"],
                    "output": ev["result"],
                })
            elif t == "finish":
                break
    finally:
        yield _sse({"type": "finish-step"})
        yield _sse({"type": "finish"})
        yield _sse("[DONE]")
