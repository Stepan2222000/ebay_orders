"""FastAPI: приём скриншотов в очередь стадии A + чат стадии B + сайдбар."""
import asyncio
import base64
import json
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from .agent import event_to_part, run_agent_session
from .config import settings
from .db import close, pool
from .stream import SSE_HEADERS, encode_ui_message_stream
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
# Локальный фронт-сервер dev (Next.js) хочет ходить на FastAPI с другого
# origin. SSE через Next.js rewrites буферизуется, поэтому в dev фронт ходит
# напрямую — нужен CORS. В prod фронт лежит как static на этом же FastAPI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3050",
        "http://127.0.0.1:3050",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
api = APIRouter(prefix="/api")


# ─── Sidebar: статус транскрибации ─────────────────────────────────────────

_STATUS_SQL = (
    "SELECT ocr_status::text AS s, count(*)::int AS n "
    "FROM screenshots GROUP BY ocr_status"
)
# Снимок считается «в сборке агентом» от момента когда OCR закрыл его
# до момента когда стадия B либо привязала к заказу (agent_status='done'),
# либо явно отметила как failed.
_ASSEMBLING_SQL = (
    "SELECT count(*)::int FROM screenshots "
    "WHERE ocr_status='done' AND agent_status IN ('pending','running')"
)


async def _status_dict(conn: asyncpg.Connection) -> dict[str, int]:
    out = {"pending": 0, "running": 0, "done": 0, "failed": 0, "assembling": 0}
    for r in await conn.fetch(_STATUS_SQL):
        out[r["s"]] = r["n"]
    out["assembling"] = await conn.fetchval(_ASSEMBLING_SQL) or 0
    return out


@api.get("/status")
async def status():
    async with (await pool()).acquire() as conn:
        return await _status_dict(conn)


@api.get("/status/stream")
async def status_stream(request: Request):
    """SSE: текущие counts → подписка на NOTIFY status_changed → пуш на каждое."""

    async def gen():
        # выделенное соединение, чтобы LISTEN не держал слот пула
        conn = await asyncpg.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            user=settings.pg_user,
            password=settings.pg_password,
            database=settings.pg_database,
        )
        queue: asyncio.Queue = asyncio.Queue()

        def on_notify(_c, _pid, _ch, _payload):
            queue.put_nowait(None)

        try:
            await conn.add_listener("status_changed", on_notify)
            yield f"data: {json.dumps(await _status_dict(conn))}\n\n"
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # keepalive + раннее обнаружение disconnect: write упадёт
                    yield ": ping\n\n"
                    continue
                # пакетим всплеск нотификаций в один push
                while not queue.empty():
                    queue.get_nowait()
                yield f"data: {json.dumps(await _status_dict(conn))}\n\n"
        finally:
            await conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─── Stage A: загрузка скриншотов + сайдбар ────────────────────────────────


def _parse_sha(sha: str) -> bytes:
    try:
        b = bytes.fromhex(sha)
    except ValueError:
        raise HTTPException(400, "sha256: ожидается hex")
    if len(b) != 32:
        raise HTTPException(400, "sha256: должно быть 64 hex-символа")
    return b


@api.post("/screenshots")
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


@api.get("/screenshots")
async def list_screenshots():
    rows = await (await pool()).fetch(
        """
        SELECT
            encode(s.sha256, 'hex') AS sha,
            s.ocr_status::text       AS ocr_status,
            s.agent_status::text     AS agent_status,
            s.last_error,
            s.byte_size,
            s.mime_type,
            s.created_at,
            o.order_number
        FROM screenshots s
        LEFT JOIN orders o ON o.order_id = s.order_id
        ORDER BY s.created_at DESC
        """
    )
    return {
        "screenshots": [
            {**dict(r), "created_at": r["created_at"].isoformat()}
            for r in rows
        ]
    }


@api.get("/screenshots/{sha}")
async def screenshot_detail(sha: str):
    sha_b = _parse_sha(sha)
    row = await (await pool()).fetchrow(
        """
        SELECT
            encode(s.sha256, 'hex') AS sha,
            s.ocr_status::text       AS ocr_status,
            s.agent_status::text     AS agent_status,
            s.last_error,
            s.byte_size,
            s.mime_type,
            s.created_at,
            o.order_number,
            r.raw_json,
            r.model    AS ocr_model,
            r.ocr_at
        FROM screenshots s
        LEFT JOIN orders o ON o.order_id = s.order_id
        LEFT JOIN raw_ocr r ON r.sha256  = s.sha256
        WHERE s.sha256 = $1
        """,
        sha_b,
    )
    if row is None:
        raise HTTPException(404, "не найдено")
    out = {**dict(row), "created_at": row["created_at"].isoformat()}
    out["ocr_at"] = row["ocr_at"].isoformat() if row["ocr_at"] is not None else None
    return out


@api.get("/screenshots/{sha}/image")
async def screenshot_image(sha: str):
    sha_b = _parse_sha(sha)
    row = await (await pool()).fetchrow(
        "SELECT bytes, mime_type FROM screenshots WHERE sha256=$1", sha_b
    )
    if row is None:
        raise HTTPException(404, "не найдено")
    return Response(
        content=row["bytes"],
        media_type=row["mime_type"],
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@api.delete("/screenshots/{sha}")
async def screenshot_delete(sha: str):
    sha_b = _parse_sha(sha)
    deleted = await (await pool()).fetchval(
        "DELETE FROM screenshots WHERE sha256=$1 RETURNING 1", sha_b
    )
    if not deleted:
        raise HTTPException(404, "не найдено")
    return {"deleted": sha}


# ─── Stage B: чат ───────────────────────────────────────────────────────────


def _decode_data_url(url: str) -> bytes | None:
    if not url.startswith("data:"):
        return None
    try:
        _, b64 = url.split(",", 1)
        return base64.b64decode(b64)
    except Exception:
        return None


def _extract_last_user_message(messages: list[dict]) -> tuple[str, list[tuple[bytes, str]], list[dict]]:
    """Берём последнее user-сообщение из UIMessage[] и достаём text + files.

    Возвращаем (text, files, parts_for_db).
    """
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if last_user is None:
        raise HTTPException(400, "нет user-сообщения в запросе")

    text_chunks: list[str] = []
    files: list[tuple[bytes, str]] = []
    parts: list[dict] = []

    for part in last_user.get("parts") or []:
        pt = part.get("type")
        if pt == "text":
            t = part.get("text", "")
            if t:
                text_chunks.append(t)
                parts.append({"type": "text", "text": t})
        elif pt == "file":
            url = part.get("url") or ""
            media = part.get("mediaType") or ""
            data = _decode_data_url(url)
            if data is None:
                continue
            if len(data) > settings.max_screenshot_bytes:
                raise HTTPException(413, "файл больше 10 МБ")
            mime = detect_mime(data) or media or "application/octet-stream"
            files.append((data, mime))
            parts.append({"type": "file", "mime": mime, "data_url": url})

    if not parts:
        raise HTTPException(400, "пустое сообщение")
    text = "\n".join(text_chunks) or None
    return text, files, parts


@api.post("/chat/messages")
async def post_chat_message(req: Request):
    body = await req.json()
    user_text, user_files, user_parts = _extract_last_user_message(body.get("messages") or [])

    p = await pool()
    await p.execute(
        "INSERT INTO chat_messages(session_id, role, parts) VALUES ('default', 'user', $1)",
        user_parts,
    )

    accumulated_parts: list[dict] = []
    aborted = {"value": False}

    async def collect_then_pass(events):
        async for ev in events:
            piece = event_to_part(ev)
            if piece is not None:
                accumulated_parts.append(piece)
            yield ev

    raw_events = run_agent_session(
        p, app.state.http,
        user_text=user_text, user_files=user_files,
    )
    sse_chunks = encode_ui_message_stream(collect_then_pass(raw_events))

    async def finalize():
        try:
            async for chunk in sse_chunks:
                yield chunk
        except asyncio.CancelledError:
            aborted["value"] = True
            raise
        finally:
            saved = list(accumulated_parts)
            if aborted["value"]:
                saved.append({"type": "text", "text": "(прервано)"})
            if saved:
                try:
                    await p.execute(
                        "INSERT INTO chat_messages(session_id, role, parts) "
                        "VALUES ('default', 'assistant', $1)",
                        saved,
                    )
                except Exception:
                    pass  # БД могла отвалиться вместе с client'ом — не маскируем абсолютно, но и не падаем здесь

    return StreamingResponse(finalize(), headers=SSE_HEADERS)


_CHAT_HISTORY_SQL = (
    "SELECT message_id, role, parts, created_at FROM chat_messages "
    "WHERE session_id='default' ORDER BY created_at, message_id"
)


async def _chat_history(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(_CHAT_HISTORY_SQL)
    return [
        {
            "id": r["message_id"],
            "role": r["role"],
            "parts": r["parts"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@api.get("/chat/messages")
async def get_chat_messages():
    async with (await pool()).acquire() as conn:
        return {"messages": await _chat_history(conn)}


@api.get("/chat/stream")
async def chat_stream(request: Request):
    """SSE: текущая история → подписка на NOTIFY chat_changed → пуш на каждое."""

    async def gen():
        conn = await asyncpg.connect(
            host=settings.pg_host,
            port=settings.pg_port,
            user=settings.pg_user,
            password=settings.pg_password,
            database=settings.pg_database,
        )
        # JSONB ↔ dict
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        queue: asyncio.Queue = asyncio.Queue()

        def on_notify(_c, _pid, _ch, _payload):
            queue.put_nowait(None)

        try:
            await conn.add_listener("chat_changed", on_notify)
            yield f"data: {json.dumps({'messages': await _chat_history(conn)}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                while not queue.empty():
                    queue.get_nowait()
                yield f"data: {json.dumps({'messages': await _chat_history(conn)}, ensure_ascii=False)}\n\n"
        finally:
            await conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@api.post("/chat/reset")
async def reset_chat():
    n = await (await pool()).fetchval(
        "WITH d AS (DELETE FROM chat_messages WHERE session_id='default' RETURNING 1) "
        "SELECT count(*) FROM d"
    )
    return {"deleted": int(n or 0)}


app.include_router(api)
