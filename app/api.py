"""FastAPI: приём скриншотов в очередь стадии A + чат стадии B."""
import base64
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .agent import run_agent_session
from .config import settings
from .db import close, pool
from .util import detect_mime, sha256


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool()
    app.state.http = httpx.AsyncClient()
    try:
        yield
    finally:
        await app.state.http.aclose()
        await close()


app = FastAPI(lifespan=lifespan)


# ─── Stage A: загрузка скриншотов ───────────────────────────────────────────

@app.post("/screenshots")
async def upload(files: list[UploadFile]):
    out = []
    p = await pool()
    async with p.acquire() as conn:
        for f in files:
            data = await f.read()
            if len(data) > settings.max_screenshot_bytes:
                raise HTTPException(413, f"{f.filename}: больше 10 МБ")
            mime = detect_mime(data)
            if mime is None:
                raise HTTPException(415, f"{f.filename}: только png/jpeg/webp/gif")
            sha = sha256(data)
            inserted = await conn.fetchval(
                "INSERT INTO screenshots(sha256, byte_size, mime_type, bytes) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (sha256) DO NOTHING RETURNING sha256",
                sha, len(data), mime, data,
            )
            out.append({
                "sha256": sha.hex(),
                "status": "queued" if inserted is not None else "duplicate",
            })
    return {"screenshots": out}


# ─── Stage B: чат ───────────────────────────────────────────────────────────

@app.post("/chat/messages")
async def post_chat_message(
    text: str = Form(default=""),
    files: list[UploadFile] | None = File(default=None),
):
    p = await pool()
    user_files: list[tuple[bytes, str]] = []
    user_parts: list[dict] = []

    if text:
        user_parts.append({"type": "text", "text": text})

    for f in files or []:
        data = await f.read()
        if len(data) > settings.max_screenshot_bytes:
            raise HTTPException(413, f"{f.filename}: больше 10 МБ")
        mime = detect_mime(data)
        if mime is None:
            raise HTTPException(415, f"{f.filename}: только png/jpeg/webp/gif")
        user_files.append((data, mime))
        data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        user_parts.append({"type": "file", "mime": mime, "data_url": data_url})

    if not user_parts:
        raise HTTPException(400, "пустое сообщение: нужен text или files")

    await p.execute(
        "INSERT INTO chat_messages(session_id, role, parts) VALUES ('default', 'user', $1)",
        user_parts,
    )

    parts = await run_agent_session(
        p, app.state.http,
        user_text=text or None,
        user_files=user_files,
    )

    await p.execute(
        "INSERT INTO chat_messages(session_id, role, parts) VALUES ('default', 'assistant', $1)",
        parts,
    )
    return {"parts": parts}


@app.get("/chat/messages")
async def get_chat_messages():
    rows = await (await pool()).fetch(
        "SELECT message_id, role, parts, created_at FROM chat_messages "
        "WHERE session_id='default' ORDER BY created_at, message_id"
    )
    return {
        "messages": [
            {
                "id": r["message_id"],
                "role": r["role"],
                "parts": r["parts"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }


@app.post("/chat/reset")
async def reset_chat():
    n = await (await pool()).fetchval(
        "WITH d AS (DELETE FROM chat_messages WHERE session_id='default' RETURNING 1) "
        "SELECT count(*) FROM d"
    )
    return {"deleted": int(n or 0)}
