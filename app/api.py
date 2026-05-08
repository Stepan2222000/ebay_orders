"""FastAPI: приём скриншотов в очередь стадии A + чат стадии B + сайдбар.

Архитектура: один диспетчер агента живёт в воркере. API-handler /chat/messages —
тонкий: записывает user-message в БД, ставит stop_requested для активной сессии
(автоматический Stop при Send), возвращает 200 OK. Воркер по NOTIFY
'user_message_arrived' просыпается и стримит ответ в parts assistant-message.
Фронт получает обновления через /api/chat/stream (SSE snapshot по chat_changed).

SSE: один общий PgFanout (app/listener.py) с одним dedicated asyncpg-коннектом
держит LISTEN на status_changed/agent_state/chat_changed. SSE-handler'ы берут
очередь из fanout и читают БД через общий pool() — никаких per-client коннектов.
"""
import asyncio
import json
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .db import close, pool
from .listener import PgFanout
from .util import detect_mime, sha256


_FANOUT_CHANNELS = ("status_changed", "agent_state", "chat_changed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool()
    app.state.http = httpx.AsyncClient()
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


# ─── helpers ────────────────────────────────────────────────────────────────

_HEARTBEAT = json.dumps({"_heartbeat": True})
_HEARTBEAT_TIMEOUT_S = 15.0


async def _drain_queue(q: asyncio.Queue) -> None:
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


# ─── Sidebar: статус транскрибации + состояние агента ──────────────────────

_STATUS_SQL = (
    "SELECT ocr_status::text AS s, count(*)::int AS n "
    "FROM screenshots GROUP BY ocr_status"
)
_ASSEMBLING_SQL = (
    "SELECT count(*)::int FROM screenshots "
    "WHERE ocr_status='done' AND agent_status IN ('pending','running')"
)
_AGENT_STATE_SQL = (
    "SELECT EXISTS (SELECT 1 FROM agent_run WHERE finished_at IS NULL) AS active, "
    "       COALESCE((SELECT thinking FROM agent_run "
    "                  WHERE finished_at IS NULL ORDER BY run_id DESC LIMIT 1), false) AS thinking"
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
        "agent_active": False, "agent_thinking": False,
        "agent_total": 0, "agent_done": 0, "agent_failed": 0,
    }
    for r in await conn.fetch(_STATUS_SQL):
        out[r["s"]] = r["n"]
    out["assembling"] = await conn.fetchval(_ASSEMBLING_SQL) or 0
    state = await conn.fetchrow(_AGENT_STATE_SQL)
    out["agent_active"] = bool(state["active"])
    out["agent_thinking"] = bool(state["thinking"])
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
    """SSE: counts + agent state. Слушает status_changed и agent_state через fanout."""
    fanout: PgFanout = request.app.state.fanout
    queue = fanout.subscribe("status_changed", "agent_state")

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


# ─── Stage B: чат ───────────────────────────────────────────────────────────


def _extract_user_parts(messages: list[dict]) -> list[dict]:
    """Берём последнее user-сообщение из UIMessage[] и переводим в БД-формат."""
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if last_user is None:
        raise HTTPException(400, "нет user-сообщения в запросе")

    parts: list[dict] = []
    for part in last_user.get("parts") or []:
        pt = part.get("type")
        if pt == "text":
            t = part.get("text", "")
            if t:
                parts.append({"type": "text", "text": t})
        elif pt == "file":
            url = part.get("url") or ""
            mime = part.get("mediaType") or "application/octet-stream"
            if not url:
                continue
            parts.append({"type": "file", "mime": mime, "data_url": url})

    if not parts:
        raise HTTPException(400, "пустое сообщение")
    return parts


@api.post("/chat/messages")
async def post_chat_message(req: Request):
    """Тонкий handler: пишет user-message и ставит автостоп текущему агенту.

    Сам ответ агента стримит воркер: по триггеру user_message_arrived создаст
    agent_run и assistant chat_message, прокрутит сессию, parts появятся
    у фронта через /api/chat/stream snapshot. Этот endpoint не возвращает
    стрим — useChat считает запрос завершённым и переключится в idle.
    """
    body = await req.json()
    user_parts = _extract_user_parts(body.get("messages") or [])

    p = await pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # Автостоп активной сессии — она аккуратно завершится с пометкой
            # «прервано» и воркер тут же стартует новую с нашим user-message.
            await conn.execute(
                "UPDATE agent_run SET stop_requested=true WHERE finished_at IS NULL"
            )
            await conn.execute(
                "INSERT INTO chat_messages(session_id, role, parts) "
                "VALUES ('default', 'user', $1)",
                user_parts,
            )
    return {"ok": True}


@api.post("/agent/stop")
async def agent_stop():
    """Запрос на прерывание текущей агентской сессии."""
    n = await (await pool()).fetchval(
        "WITH u AS ("
        " UPDATE agent_run SET stop_requested=true "
        "  WHERE finished_at IS NULL AND stop_requested=false RETURNING 1"
        ") SELECT count(*) FROM u"
    )
    return {"stopped": int(n or 0)}


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


async def _chat_snapshot() -> str:
    async with (await pool()).acquire() as conn:
        return json.dumps({"messages": await _chat_history(conn)}, ensure_ascii=False)


@api.get("/chat/messages")
async def get_chat_messages():
    async with (await pool()).acquire() as conn:
        return {"messages": await _chat_history(conn)}


@api.get("/chat/stream")
async def chat_stream(request: Request):
    """SSE: текущая история → пушим snapshot на каждый chat_changed."""
    fanout: PgFanout = request.app.state.fanout
    queue = fanout.subscribe("chat_changed")

    async def gen():
        try:
            yield f"data: {await _chat_snapshot()}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_TIMEOUT_S)
                    await _drain_queue(queue)
                    yield f"data: {await _chat_snapshot()}\n\n"
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


@api.post("/chat/reset")
async def reset_chat():
    n = await (await pool()).fetchval(
        "WITH d AS (DELETE FROM chat_messages WHERE session_id='default' RETURNING 1) "
        "SELECT count(*) FROM d"
    )
    return {"deleted": int(n or 0)}


app.include_router(api)
