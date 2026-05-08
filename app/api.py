"""FastAPI: приём скриншотов + GET истории чата + сайдбар-стрим.

Сейчас здесь нет POST стадии B — он будет добавлен следующим этапом.
Из работающего: загрузка/просмотр скриншотов, GET истории чата для useChat
initialMessages, SSE со счётчиками OCR + agent_total/done/failed.

SSE: один общий PgFanout (app/listener.py) с одним dedicated asyncpg-коннектом
держит LISTEN на status_changed. Подписчики читают БД через общий pool().
"""
import asyncio
import json
from contextlib import asynccontextmanager

import asyncpg
from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .db import close, pool
from .listener import PgFanout
from .util import detect_mime, sha256


_FANOUT_CHANNELS = ("status_changed",)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool()
    app.state.fanout = PgFanout(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        database=settings.pg_database,
        channels=_FANOUT_CHANNELS,
    )
    await app.state.fanout.start()
    try:
        yield
    finally:
        await app.state.fanout.close()
        await close()


app = FastAPI(lifespan=lifespan)
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


# ─── helpers ────────────────────────────────────────────────────────────────

_HEARTBEAT = json.dumps({"_heartbeat": True})
_HEARTBEAT_TIMEOUT_S = 15.0


async def _drain_queue(q: asyncio.Queue) -> None:
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


# ─── Sidebar / шапка чата: счётчики OCR + прогресс агента ───────────────────

_STATUS_SQL = (
    "SELECT ocr_status::text AS s, count(*)::int AS n "
    "FROM screenshots GROUP BY ocr_status"
)
_ASSEMBLING_SQL = (
    "SELECT count(*)::int FROM screenshots "
    "WHERE ocr_status='done' AND agent_status IN ('pending','running')"
)
_AGENT_COUNTS_SQL = (
    "SELECT "
    " count(*) FILTER (WHERE ocr_status='done' "
    "   AND agent_status IN ('pending','running','done','failed'))::int AS total, "
    " count(*) FILTER (WHERE agent_status='done')::int AS done, "
    " count(*) FILTER (WHERE ocr_status='done' AND agent_status='failed')::int AS failed "
    "FROM screenshots"
)


async def _status_dict(conn: asyncpg.Connection) -> dict[str, object]:
    out: dict[str, object] = {
        "pending": 0, "running": 0, "done": 0, "failed": 0,
        "assembling": 0,
        "agent_total": 0, "agent_done": 0, "agent_failed": 0,
    }
    for r in await conn.fetch(_STATUS_SQL):
        out[r["s"]] = r["n"]
    out["assembling"] = await conn.fetchval(_ASSEMBLING_SQL) or 0
    counts = await conn.fetchrow(_AGENT_COUNTS_SQL)
    out["agent_total"] = counts["total"]
    out["agent_done"] = counts["done"]
    out["agent_failed"] = counts["failed"]
    return out


async def _status_snapshot() -> str:
    async with (await pool()).acquire() as conn:
        return json.dumps(await _status_dict(conn))


@api.get("/status")
async def status():
    async with (await pool()).acquire() as conn:
        return await _status_dict(conn)


@api.get("/status/stream")
async def status_stream(request: Request):
    """SSE: счётчики OCR + agent_total/done/failed. Слушает status_changed."""
    fanout: PgFanout = request.app.state.fanout
    queue = fanout.subscribe("status_changed")

    async def gen():
        try:
            yield f"data: {await _status_snapshot()}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_TIMEOUT_S)
                    await _drain_queue(queue)
                    yield f"data: {await _status_snapshot()}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {_HEARTBEAT}\n\n"
        finally:
            fanout.unsubscribe(queue)

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


# ─── Stage B: история чата (POST стадии B будет добавлен на следующем шаге) ─

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


@api.post("/chat/reset")
async def reset_chat():
    n = await (await pool()).fetchval(
        "WITH d AS (DELETE FROM chat_messages WHERE session_id='default' RETURNING 1) "
        "SELECT count(*) FROM d"
    )
    return {"deleted": int(n or 0)}


app.include_router(api)
