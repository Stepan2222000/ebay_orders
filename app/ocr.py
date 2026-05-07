"""Стадия A — один скриншот → сырой JSON.

Один публичный вызов: ocr.transcribe(bytes, mime, http_client).
Все ошибки — OcrError с короткой человеческой причиной.
"""
import base64
import json
import time
from dataclasses import dataclass

import httpx

from .config import settings
from .prompt import SYSTEM_PROMPT_STAGE_A
from .schema import RAW_OCR_SCHEMA


class OcrError(Exception):
    """Любая ошибка стадии A. Текст — пригоден для last_error в БД."""


@dataclass(frozen=True)
class OcrResult:
    raw_json: dict
    model: str
    cost_usd: float | None
    latency_s: float


def _payload(image_bytes: bytes, mime: str) -> dict:
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {
        "model": settings.openrouter_model,
        "max_tokens": settings.openrouter_max_tokens,
        "reasoning": {"enabled": False},
        "provider": {"ignore": list(settings.openrouter_ignore_providers)},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "ebay_raw_ocr",
                "strict": True,
                "schema": RAW_OCR_SCHEMA,
            },
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_STAGE_A},
            {"role": "user", "content": [
                {"type": "text", "text": "Распознай этот скриншот."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }


async def transcribe(image_bytes: bytes, mime: str, http: httpx.AsyncClient) -> OcrResult:
    t0 = time.monotonic()
    try:
        r = await http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=_payload(image_bytes, mime),
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            timeout=settings.openrouter_timeout_s,
        )
    except (httpx.TimeoutException, httpx.RequestError) as e:
        raise OcrError(f"network: {type(e).__name__}: {e}") from e

    if r.status_code != 200:
        raise OcrError(f"HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    msg = data["choices"][0]["message"]
    content = msg.get("content")
    if not content:
        reasoning_len = len(msg.get("reasoning") or "")
        raise OcrError(f"empty content (reasoning_len={reasoning_len})")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise OcrError(f"invalid JSON: {e}") from e

    return OcrResult(
        raw_json=parsed,
        model=data.get("model", settings.openrouter_model),
        cost_usd=(data.get("usage") or {}).get("cost"),
        latency_s=time.monotonic() - t0,
    )
