from __future__ import annotations

import base64
import json
import time

from openai import AsyncOpenAI

from app.config import settings
from app.schema import RawOcrPayload, to_strict_schema
from app.stage_a.prompt import SYSTEM_PROMPT, USER_PROMPT

_STRICT_SCHEMA = to_strict_schema(RawOcrPayload)


class OcrClient:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            timeout=settings.STAGE_A_TIMEOUT_SECONDS,
            max_retries=0,
        )

    async def recognize(self, png_bytes: bytes) -> tuple[RawOcrPayload, int]:
        data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"
        t0 = time.perf_counter()
        resp = await self._client.chat.completions.create(
            model=settings.STAGE_A_MODEL,
            temperature=0,
            max_tokens=16000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "raw_ocr",
                    "strict": True,
                    "schema": _STRICT_SCHEMA,
                },
            },
            extra_body={
                "reasoning": {"enabled": False},
                # Inceptron has been observed to loop on certain screenshots
                # without ever stopping; exclude it from provider routing.
                "provider": {"ignore": ["Inceptron"]},
            },
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        ms = int((time.perf_counter() - t0) * 1000)
        content = resp.choices[0].message.content or ""
        payload = RawOcrPayload.model_validate(json.loads(content))
        return payload, ms

    async def aclose(self) -> None:
        await self._client.close()
