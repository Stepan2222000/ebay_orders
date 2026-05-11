"""FastAPI: приём скриншотов + чат стадии B + сайдбар-стрим.

Эндпоинты:
- POST /api/screenshots, GET/DELETE /api/screenshots/{sha} — стадия A.
- GET /api/status, GET /api/status/stream — счётчики OCR + agent_*.
- GET /api/chat/messages, POST /api/chat/reset — история чата.
- POST /api/chat — стадия B: SSE поток UIMessageStream v1
  (см. app/agent.py:stream_stage_b и app/stream.py).

SSE статуса: один общий PgFanout (app/listener.py) с dedicated
asyncpg-коннектом держит LISTEN на status_changed. Подписчики читают
БД через общий pool().
"""
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

import anyio
import asyncpg
import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from .agent import stream_stage_b
from .config import settings
from .db import close, dump_schema_text, pool
from .listener import PgFanout
from .prompt import SYSTEM_PROMPT_STAGE_B
from .stream import SSE_HEADERS, sse
from .util import detect_mime, sha256

log = logging.getLogger(__name__)


_FANOUT_CHANNELS = ("status_changed",)


@asynccontextmanager
async def lifespan(app: FastAPI):
    p = await pool()
    async with p.acquire() as conn:
        app.state.db_schema = await dump_schema_text(conn)
    app.state.http = httpx.AsyncClient(timeout=settings.openrouter_timeout_s)
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


_cors_origins_raw = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3050,http://127.0.0.1:3050",
)
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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


_SNIPPET_PAD = 60
_SEARCH_LIMIT = 500


def _match_snippet(query: str, *texts: str | None) -> str | None:
    """Найти первое вхождение query в любом из text-полей (case-insensitive).
    Вернуть кусок ±_SNIPPET_PAD символов с многоточиями по краям."""
    needle = query.lower()
    for src in texts:
        if not src:
            continue
        i = src.lower().find(needle)
        if i < 0:
            continue
        start = max(0, i - _SNIPPET_PAD)
        end = min(len(src), i + len(query) + _SNIPPET_PAD)
        snippet = src[start:end].replace("\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(src):
            snippet = snippet + "…"
        return snippet
    return None


@api.get("/screenshots")
async def list_screenshots(q: str | None = None):
    q_clean = (q or "").strip()
    if not q_clean:
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

    pat = f"%{q_clean}%"
    rows = await (await pool()).fetch(
        """
        SELECT
            encode(s.sha256, 'hex')          AS sha,
            s.ocr_status::text                AS ocr_status,
            s.agent_status::text              AS agent_status,
            s.last_error,
            s.byte_size,
            s.mime_type,
            s.created_at,
            o.order_number,
            o.sold_by,
            r.raw_json->>'visible_text'       AS visible_text
        FROM screenshots s
        LEFT JOIN orders  o ON o.order_id = s.order_id
        LEFT JOIN raw_ocr r ON r.sha256   = s.sha256
        WHERE r.raw_json::text ILIKE $1
           OR o.order_number   ILIKE $1
           OR o.sold_by        ILIKE $1
        ORDER BY s.created_at DESC
        LIMIT $2
        """,
        pat, _SEARCH_LIMIT,
    )

    out = []
    for r in rows:
        d = dict(r)
        snippet = _match_snippet(
            q_clean, d.pop("visible_text", None), d.pop("sold_by", None), d.get("order_number"),
        )
        d["match"] = snippet
        d["created_at"] = r["created_at"].isoformat()
        out.append(d)
    return {"screenshots": out}


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


@api.post("/screenshots/{sha}/retry")
async def screenshot_retry(sha: str):
    """Повторно прогнать стадию A.

    Что делает:
    - если снимок привязан к заказу — удаляет сам заказ (каскад снимет
      товары/возвраты/треки, FK SET NULL отвяжет все его скриншоты),
      затем возвращает остальные скриншоты этого заказа в очередь сборки
      (agent_status='pending'), чтобы агент пересобрал заказ заново;
    - удаляет raw_ocr этого снимка;
    - сбрасывает ocr_status/agent_status в pending, last_error в NULL.

    Воркер сам подберёт снимок.
    """
    sha_b = _parse_sha(sha)
    p = await pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT order_id FROM screenshots WHERE sha256=$1 FOR UPDATE",
                sha_b,
            )
            if row is None:
                raise HTTPException(404, "не найдено")
            old_order_id = row["order_id"]

            if old_order_id is not None:
                # источник для audit_trg, чтобы DELETE заказа отметился в журнале
                await conn.execute(
                    "SELECT set_config('app.source', 'user_chat', true)"
                )
                sibling_shas = await conn.fetch(
                    "SELECT sha256 FROM screenshots "
                    "WHERE order_id=$1 AND sha256 <> $2",
                    old_order_id, sha_b,
                )
                # каскад снесёт items/refunds/tracks; FK SET NULL обнулит
                # screenshots.order_id для всех связанных снимков
                await conn.execute(
                    "DELETE FROM orders WHERE order_id=$1", old_order_id
                )
                if sibling_shas:
                    await conn.execute(
                        "UPDATE screenshots SET agent_status='pending' "
                        "WHERE sha256 = ANY($1::bytea[])",
                        [r["sha256"] for r in sibling_shas],
                    )

            await conn.execute("DELETE FROM raw_ocr WHERE sha256=$1", sha_b)
            await conn.execute(
                "UPDATE screenshots SET "
                "ocr_status='pending', agent_status='pending', "
                "last_error=NULL, order_id=NULL "
                "WHERE sha256=$1",
                sha_b,
            )
    return {"retried": sha, "deleted_order_id": old_order_id}


# ─── Stage B: история чата + POST /api/chat ──────────────────────────────

_CHAT_HISTORY_SQL = (
    "SELECT message_id, role, parts, created_at FROM chat_messages "
    "WHERE session_id='default' ORDER BY created_at, message_id"
)
_PENDING_COUNT_SQL = (
    "SELECT count(*)::int FROM screenshots "
    "WHERE ocr_status='done' AND agent_status='pending'"
)
_INSERT_CHAT_MSG_SQL = (
    "INSERT INTO chat_messages(session_id, role, parts) "
    "VALUES('default', $1, $2)"
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


def _user_for_model(parts: list[dict]) -> dict | None:
    """Сложить user-сообщение в OpenAI multimodal-формат.

    text-парты → {type:"text", text:...}; file-парты с image/* → image_url
    с data-URL. Если есть только текст — content становится строкой.
    """
    text_chunks: list[str] = []
    image_urls: list[str] = []
    for p in parts:
        if p.get("type") == "text" and p.get("text"):
            text_chunks.append(p["text"])
        elif p.get("type") == "file":
            mt = p.get("mediaType") or p.get("mime") or ""
            url = p.get("url") or p.get("data_url")
            if url and mt.startswith("image/"):
                image_urls.append(url)
    if not text_chunks and not image_urls:
        return None
    if not image_urls:
        return {"role": "user", "content": "\n".join(text_chunks)}
    content: list[dict] = []
    if text_chunks:
        content.append({"type": "text", "text": "\n".join(text_chunks)})
    for u in image_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})
    return {"role": "user", "content": content}


def _project_history_for_model(history: list[dict]) -> list[dict]:
    """Формат для OpenAI/OpenRouter: assistant — text-only (без tool plates),
    user — text+image_url. Пустые ассистент-сообщения пропускаются."""
    out: list[dict] = []
    for m in history:
        parts = m.get("parts") or []
        role = m["role"]
        if role == "user":
            msg = _user_for_model(parts)
            if msg is not None:
                out.append(msg)
        elif role == "assistant":
            text = "\n".join(p["text"] for p in parts if p.get("type") == "text" and p.get("text"))
            if text:
                out.append({"role": "assistant", "content": text})
        # system — не сохраняем в чат, не возвращаем
    return out


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


@api.post("/chat")
async def chat_post(request: Request):
    payload = await request.json()
    user_msg = payload.get("message") or {}
    user_parts = user_msg.get("parts") or []

    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(_INSERT_CHAT_MSG_SQL, "user", user_parts)
        history = await _chat_history(conn)
        pending_count = await conn.fetchval(_PENDING_COUNT_SQL) or 0

    messages_for_model: list[dict] = [{
        "role": "system",
        "content": SYSTEM_PROMPT_STAGE_B.format(
            pending_count=pending_count,
            db_schema=request.app.state.db_schema,
        ),
    }]
    messages_for_model.extend(_project_history_for_model(history))
    # последнее user-сообщение уже в history (только что INSERT'нули) — не дублируем

    message_id = f"msg_{uuid.uuid4().hex}"
    parts_acc: list[dict] = []
    cancelled = False

    async def gen():
        nonlocal cancelled
        try:
            async for ev in stream_stage_b(
                p, request.app.state.http, messages_for_model, message_id, parts_acc,
            ):
                yield sse(ev)
            yield sse("[DONE]")
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            with anyio.CancelScope(shield=True):
                if cancelled:
                    parts_acc.append({"type": "text", "text": "(прервано)"})
                if parts_acc:
                    try:
                        async with p.acquire() as c:
                            await c.execute(
                                _INSERT_CHAT_MSG_SQL, "assistant", parts_acc,
                            )
                    except Exception as e:
                        log.error("failed to persist assistant msg: %s", e)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


app.include_router(api)
