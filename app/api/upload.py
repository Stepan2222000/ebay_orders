from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request, UploadFile

from app.schema import UploadResultItem

router = APIRouter()


@router.post("/api/upload", response_model=list[UploadResultItem])
async def upload(request: Request, files: list[UploadFile]) -> list[UploadResultItem]:
    pool = request.app.state.pool
    out: list[UploadResultItem] = []
    async with pool.acquire() as conn:
        for f in files:
            data = await f.read()
            sha = hashlib.sha256(data).hexdigest()
            mime = f.content_type or "image/png"
            inserted = await conn.fetchval(
                """
                INSERT INTO screenshots (sha256, bytes, mime, byte_size)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING sha256
                """,
                sha,
                data,
                mime,
                len(data),
            )
            if inserted is None:
                await conn.execute(
                    """
                    UPDATE screenshots
                       SET agent_status = 'pending',
                           ocr_status = CASE
                               WHEN ocr_status = 'failed' THEN 'pending'::ocr_status_t
                               ELSE ocr_status
                           END,
                           error_text = NULL
                     WHERE sha256 = $1
                    """,
                    sha,
                )
            out.append(
                UploadResultItem(sha256=sha, is_new=inserted is not None, byte_size=len(data))
            )
    return out
