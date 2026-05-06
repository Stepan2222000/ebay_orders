from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.api import chat as chat_api
from app.api import upload
from app.config import settings
from app.ocr_client import OcrClient
from app.stage_a.worker import stage_a_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await db.make_pool()
    client = OcrClient()
    stop = asyncio.Event()
    task = asyncio.create_task(
        stage_a_loop(
            pool,
            client,
            settings.STAGE_A_CONCURRENCY,
            settings.STAGE_A_POLL_SECONDS,
            settings.STAGE_A_MODEL,
            stop,
        ),
        name="stage_a_loop",
    )
    app.state.pool = pool
    app.state.client = client
    log.info("startup complete")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await client.aclose()
        await pool.close()
        log.info("shutdown complete")


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3100",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3100",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(upload.router)
app.include_router(chat_api.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
