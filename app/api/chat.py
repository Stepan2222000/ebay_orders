"""POST /api/chat — Stage B agent loop, streamed via AI SDK v6 protocol.

Request body:
    {
      "messages": UIMessage[],         # from useChat
      "session_id": uuid                # minted on the page
    }

Response:
    text/event-stream + x-vercel-ai-ui-message-stream: v1
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.stage_b import loop as stage_b_loop
from app.stage_b.prompt import SYSTEM_SCREENSHOT, SYSTEM_TEXT
from app.stage_b.stream import SSE_HEADERS

log = logging.getLogger("api.chat")
router = APIRouter()


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    session_id: str
    uploaded_sha256s: list[str] = Field(default_factory=list)


class ChatResetRequest(BaseModel):
    session_id: str


def _last_user_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg
    return None


def _flatten_text(parts: list[dict[str, Any]] | None) -> str:
    if not parts:
        return ""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _has_file_parts(parts: list[dict[str, Any]] | None) -> bool:
    return any(p.get("type") == "file" for p in (parts or []))


async def _wait_for_uploaded_ocr(pool: Any, sha256s: list[str]) -> None:
    if not sha256s:
        return

    deadline = (
        asyncio.get_running_loop().time()
        + settings.STAGE_A_TIMEOUT_SECONDS
        + 30
    )
    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT sha256::text AS sha256, ocr_status::text AS ocr_status
                  FROM screenshots
                 WHERE sha256::text = ANY($1::text[])
                """,
                sha256s,
            )

        statuses = {r["sha256"]: r["ocr_status"] for r in rows}
        if statuses and all(
            statuses.get(sha) in {"done", "failed"}
            for sha in sha256s
        ):
            return

        if asyncio.get_running_loop().time() >= deadline:
            return

        await asyncio.sleep(0.5)


@router.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    try:
        req = ChatRequest.model_validate(body)
    except ValidationError as e:
        return StreamingResponse(
            iter([json.dumps({"error": e.errors()}).encode()]),
            status_code=400,
        )

    pool = request.app.state.pool
    session_id = req.session_id
    last_user = _last_user_message(req.messages)
    user_text = _flatten_text(last_user.get("parts") if last_user else None)

    # Lazy-create chat session and persist user message.
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                _ = uuid.UUID(session_id)
            except ValueError:
                return StreamingResponse(
                    iter([json.dumps({"error": "invalid session_id"}).encode()]),
                    status_code=400,
                )
            await conn.execute(
                "INSERT INTO chat_sessions (session_id) VALUES ($1) "
                "ON CONFLICT (session_id) DO NOTHING",
                session_id,
            )
            if last_user is not None:
                await conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content, parts) "
                    "VALUES ($1, 'user', $2, $3::jsonb)",
                    session_id,
                    user_text or None,
                    json.dumps(last_user.get("parts") or []),
                )

    uploaded_sha256s = [
        sha for sha in req.uploaded_sha256s
        if isinstance(sha, str) and sha
    ]
    await _wait_for_uploaded_ocr(pool, uploaded_sha256s)

    # Decide branch.
    pending_rows: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        if uploaded_sha256s:
            rows = await conn.fetch(
                """
                SELECT s.sha256, r.payload
                  FROM screenshots s
                  JOIN raw_ocr r USING (sha256)
                 WHERE s.sha256::text = ANY($1::text[])
                   AND s.agent_status = 'pending'
                   AND s.ocr_status = 'done'
                 ORDER BY s.created_at
                """,
                uploaded_sha256s,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT s.sha256, r.payload
                  FROM screenshots s
                  JOIN raw_ocr r USING (sha256)
                 WHERE s.agent_status = 'pending'
                   AND s.ocr_status = 'done'
                 ORDER BY s.created_at
                """
            )
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        pending_rows.append({"sha256": r["sha256"], **payload})

    user_has_files = _has_file_parts(last_user.get("parts") if last_user else None)
    if pending_rows and (user_has_files or uploaded_sha256s):
        branch = "screenshot"
        system_prompt = SYSTEM_SCREENSHOT
        user_msg = (
            f"Пачка из {len(pending_rows)} новых снимков. "
            f"Собери заказы. Контекст:\n\n"
            + json.dumps(pending_rows, ensure_ascii=False, indent=2, default=str)
        )
    else:
        branch = "text"
        system_prompt = SYSTEM_TEXT
        user_msg = user_text or "(пустое сообщение)"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    progress = {
        "branch": branch,
        "pending_screenshots": len(pending_rows),
        "tool_calls": 0,
    }

    async def gen():
        async for chunk in stage_b_loop.run(
            messages,
            branch=branch,
            pool=pool,
            last_user_text=user_text,
            progress=progress,
        ):
            yield chunk
        # Persist assistant final text after the stream — extract it from
        # the conventional last entry the loop appended.
        final = ""
        for msg in reversed(messages):
            if msg.get("role") == "_final_text":
                final = msg.get("content", "")
                break
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO chat_messages (session_id, role, content) "
                    "VALUES ($1, 'assistant', $2)",
                    session_id,
                    final,
                )
        except Exception:
            log.exception("failed to persist assistant message")

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api/chat/reset")
async def reset_chat(request: Request) -> dict[str, bool]:
    body = await request.json()
    try:
        req = ChatResetRequest.model_validate(body)
        session_id = str(uuid.UUID(req.session_id))
    except (ValidationError, ValueError):
        return {"ok": False}

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_sessions WHERE session_id = $1",
            session_id,
        )

    return {"ok": True}
