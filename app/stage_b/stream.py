"""Helpers for emitting AI SDK v6 UI Message Stream Protocol chunks."""

from __future__ import annotations

import json
import uuid
from typing import Any


def sse(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def chunk_start(message_id: str | None = None) -> bytes:
    return sse({"type": "start", "messageId": message_id or f"msg-{uuid.uuid4().hex}"})


def chunk_start_step() -> bytes:
    return sse({"type": "start-step"})


def chunk_finish_step() -> bytes:
    return sse({"type": "finish-step"})


def chunk_finish() -> bytes:
    return sse({"type": "finish"})


def chunk_done() -> bytes:
    return b"data: [DONE]\n\n"


def chunk_text_start(text_id: str) -> bytes:
    return sse({"type": "text-start", "id": text_id})


def chunk_text_delta(text_id: str, delta: str) -> bytes:
    return sse({"type": "text-delta", "id": text_id, "delta": delta})


def chunk_text_end(text_id: str) -> bytes:
    return sse({"type": "text-end", "id": text_id})


def chunk_data(part_id: str, kind: str, data: dict[str, Any]) -> bytes:
    """Emit a `data-<kind>` part addressable by `id` for live updates."""
    return sse({"type": f"data-{kind}", "id": part_id, "data": data})


SSE_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
