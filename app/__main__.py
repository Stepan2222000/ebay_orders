"""Entry point: python -m app {api|worker|ocr-one <file>}."""
import asyncio
import json
import os
import sys

import uvicorn
from openai import AsyncOpenAI

from .config import settings
from .ocr import OcrError, transcribe
from .util import detect_mime
from .worker import run as worker_run

USAGE = "usage: python -m app {api|worker|ocr-one <file>}"


async def _ocr_one(path: str) -> int:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    mime = detect_mime(data)
    if mime is None:
        print(f"{path}: not png/jpeg/webp/gif", file=sys.stderr)
        return 3
    async with AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        timeout=settings.llm_timeout_s,
    ) as client:
        try:
            res = await transcribe(data, mime, client)
        except OcrError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 4
    print(json.dumps({
        "model": res.model,
        "latency_s": round(res.latency_s, 2),
        "raw_json": res.raw_json,
    }, indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "api":
        port = int(os.environ.get("API_PORT", "8000"))
        uvicorn.run("app.api:app", host="0.0.0.0", port=port)
    elif cmd == "worker":
        asyncio.run(worker_run())
    elif cmd == "ocr-one":
        if len(sys.argv) < 3:
            print(USAGE, file=sys.stderr)
            sys.exit(1)
        sys.exit(asyncio.run(_ocr_one(sys.argv[2])))
    else:
        print(USAGE, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
