"""Стадия A — один скриншот → сырой JSON.

Один публичный вызов: ocr.transcribe(bytes, mime, client).
Все ошибки — OcrError с короткой человеческой причиной.
"""
import base64
import json
import logging
import time
from dataclasses import dataclass

from openai import APIError, AsyncOpenAI

from .config import settings
from .prompt import SYSTEM_PROMPT_STAGE_A
from .schema import RAW_OCR_SCHEMA
from .validation import bad_tracking_reason, is_valid_order_number

log = logging.getLogger(__name__)


class OcrError(Exception):
    """Любая ошибка стадии A. Текст — пригоден для last_error в БД."""


@dataclass(frozen=True)
class OcrResult:
    raw_json: dict
    model: str
    latency_s: float


def _messages(image_bytes: bytes, mime: str) -> list[dict]:
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT_STAGE_A},
        {"role": "user", "content": [
            {"type": "text", "text": "Распознай этот скриншот."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]


def _ocr_suspect(raw: dict) -> str | None:
    """Структурно-подозрительный разбор → причина для ретрая, иначе None.

    Проверяем поля, где известный режим сбоя vision (дроп/подмена цифры)
    ломает жёсткий формат и почти наверняка означает ошибку распознавания:
      - order_number: есть, но не лёг в NN-NNNNN-NNNNN;
      - tracking_numbers: похоже на испорченный UPS/USPS (см. bad_tracking_reason).
    Пустые значения не подозрительны (обрезанный скрин — легитимный случай).
    Все случаи — hard: исчерпали ретраи → снимок в failed, кривой ключ не пишем.
    """
    if raw.get("is_order_details") is not True:
        return None
    obs = raw.get("observed") or {}
    onum = obs.get("order_number")
    if onum and not is_valid_order_number(onum):
        return f"order_number format invalid: {onum!r}"
    for t in (obs.get("tracking_numbers") or []):
        reason = bad_tracking_reason(t)
        if reason:
            return reason
    return None


# Claude через OpenAI-совместимый прокси ИГНОРИРУЕТ response_format (офиц. доки
# Anthropic «OpenAI SDK compatibility»: response_format — Ignored). Надёжный
# способ получить JSON по схеме — описать схему как единственный инструмент и
# принудительно вызвать его через tool_choice; JSON приходит в
# tool_calls[0].function.arguments, прозы модель не выдаёт. Работает и для
# моделей без structured outputs (gpt55/Claude через прокси).
_OCR_TOOLS = [{
    "type": "function",
    "function": {
        "name": "emit_ocr",
        "description": "Вернуть распознанные поля скриншота строго по схеме.",
        "parameters": RAW_OCR_SCHEMA,
    },
}]
_OCR_TOOL_CHOICE = {"type": "function", "function": {"name": "emit_ocr"}}


async def _request_ocr(image_bytes: bytes, mime: str, client: AsyncOpenAI) -> tuple[str, dict]:
    """Один vision-вызов стадии A → (model, parsed_raw_json). Бросает OcrError."""
    try:
        r = await client.chat.completions.create(
            model=settings.ocr_model,
            max_tokens=settings.llm_max_tokens,
            tools=_OCR_TOOLS,
            tool_choice=_OCR_TOOL_CHOICE,
            messages=_messages(image_bytes, mime),
        )
    except APIError as e:
        raise OcrError(f"llm: {type(e).__name__}: {str(e)[:300]}") from e

    tool_calls = r.choices[0].message.tool_calls
    if not tool_calls:
        raise OcrError("no tool_call in response")
    args = tool_calls[0].function.arguments
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as e:
        raise OcrError(f"invalid JSON: {e}") from e
    return r.model, parsed


async def transcribe(image_bytes: bytes, mime: str, client: AsyncOpenAI) -> OcrResult:
    """Стадия A с retry-on-validation: если order_number не лёг в формат —
    перечитываем снимок (ошибка vision вероятностная). Все попытки мимо —
    OcrError, снимок уходит в failed на ручной разбор, а не пишет кривой ключ."""
    t0 = time.monotonic()
    last_suspect: str | None = None
    for attempt in range(1, settings.ocr_max_attempts + 1):
        model, parsed = await _request_ocr(image_bytes, mime, client)
        last_suspect = _ocr_suspect(parsed)
        if last_suspect is None:
            return OcrResult(
                raw_json=parsed,
                model=model,
                latency_s=time.monotonic() - t0,
            )
        log.warning(
            "ocr suspect, retry %d/%d: %s",
            attempt, settings.ocr_max_attempts, last_suspect,
        )
    raise OcrError(f"{last_suspect} (after {settings.ocr_max_attempts} attempts)")
