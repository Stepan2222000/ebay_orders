"""FastAPI: приём скриншотов в очередь стадии A."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile

from .config import settings
from .db import close, pool
from .util import detect_mime, sha256


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool()
    yield
    await close()


app = FastAPI(lifespan=lifespan)


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
            existed = await conn.fetchval(
                "SELECT 1 FROM screenshots WHERE sha256 = $1", sha
            )
            if existed:
                out.append({"sha256": sha.hex(), "status": "duplicate"})
                continue
            await conn.execute(
                "INSERT INTO screenshots(sha256, byte_size, mime_type, bytes) "
                "VALUES ($1, $2, $3, $4)",
                sha, len(data), mime, data,
            )
            out.append({"sha256": sha.hex(), "status": "queued"})
    return {"screenshots": out}
