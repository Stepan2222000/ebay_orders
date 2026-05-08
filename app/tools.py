"""Инструменты стадии B: sql и save_order.

OPENAI_TOOLS — спецификация для tool-calling.
execute_sql / execute_save_order — серверные обработчики.
Источник изменений (`SET LOCAL app.source = ...`) выставляется на уровне инструмента,
аудит-триггер из миграции 001 подхватит его автоматически.
"""
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import asyncpg


# ─── JSON-Schema спецификации для OpenRouter / OpenAI tool-calling ────────────

OPENAI_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "sql",
            "description": (
                "Выполнить один SQL-запрос над БД заказов. "
                "Каждый вызов — отдельная транзакция с source='user_chat'. "
                "Используй для чтения, точечных правок, удаления заказов. "
                "Запрещено: DDL, BEGIN/COMMIT/ROLLBACK, обращение к pg_catalog/information_schema."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Один SQL-запрос."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_order",
            "description": (
                "Сохранить (создать или дополнить) один заказ из связанных скриншотов. "
                "Идемпотентно по order_number. Делает upsert, COALESCE-логика для опциональных полей. "
                "Привязывает указанные снимки и помечает их agent_status='done'."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "order_number", "sold_by", "order_total_usd",
                    "items", "screenshot_shas",
                ],
                "properties": {
                    "order_number": {"type": "string"},
                    "sold_by": {"type": "string"},
                    "ordered_at": {
                        "type": ["string", "null"],
                        "description": "Дата+время оформления заказа КАК ВИДНО в observed "
                                       "('May 3, 2026 at 4:23 PM' или ISO). Сервер нормализует.",
                    },
                    "order_total_usd": {"type": ["number", "string"]},
                    "item_subtotal_usd": {"type": ["number", "string", "null"]},
                    "shipping_usd":      {"type": ["number", "string", "null"]},
                    "sales_tax_usd":     {"type": ["number", "string", "null"]},
                    "delivery_status":   {"type": ["string", "null"]},
                    "delivered_date": {
                        "type": ["string", "null"],
                        "description": "Дата доставки КАК ВИДНО в observed ('Thu, May 7' или ISO). "
                                       "Сервер нормализует с учётом года из ordered_at.",
                    },
                    "arriving_by_date":  {"type": ["string", "null"],
                                          "description": "Текстовый диапазон/дата как видно."},
                    "shipping_service":  {"type": ["string", "null"]},
                    "is_untracked":      {"type": ["boolean", "null"]},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "item_number", "item_title",
                                "item_quantity", "item_line_total_usd",
                            ],
                            "properties": {
                                "item_number":         {"type": "string"},
                                "item_title":          {"type": "string"},
                                "item_quantity":       {"type": "integer"},
                                "item_line_total_usd": {"type": ["number", "string"]},
                            },
                        },
                    },
                    "refunds": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["refund_amount_usd", "refund_date", "refund_note"],
                            "properties": {
                                "refund_amount_usd": {"type": ["number", "string"]},
                                "refund_date":       {"type": ["string", "null"]},
                                "refund_note":       {"type": ["string", "null"]},
                            },
                        },
                    },
                    "tracking_numbers": {"type": "array", "items": {"type": "string"}},
                    "screenshot_shas": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "hex-представление sha256 каждого связанного снимка.",
                    },
                },
            },
        },
    },
]


# ─── Нормализация полей save_order ──────────────────────────────────────────
#
# Стадия A отдаёт суммы и даты в виде «как видно на скрине» — '$76.56',
# 'May 3, 2026 at 4:23 PM', 'Thu, May 7' и т.п. Промпт стадии B велит класть
# их в save_order как есть; парсинг делает сервер. Так устойчивее, чем
# уговаривать LLM выдавать строгие форматы.

_MONEY_RE = re.compile(r"-?\$?\s*([\d,]+(?:\.\d+)?)")
_TS_FORMATS = (
    "%b %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
)
_DATE_FORMATS_NO_YEAR = (
    "%a, %b %d",
    "%A, %B %d",
    "%b %d",
    "%B %d",
)
_DATE_FORMATS_WITH_YEAR = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%a, %b %d, %Y",
    "%A, %B %d, %Y",
)


def parse_money(v: Any) -> Decimal | None:
    """Возвращает Decimal или None.

    Принимает: Decimal/int/float/str. 'Free'/'free'/None/'' → None.
    Из строки выкусывает первое денежное число (`$1,234.50` → 1234.50).
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    if s.lower() in ("free", "free shipping"):
        return Decimal("0.00")
    m = _MONEY_RE.search(s)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def parse_timestamp(v: Any) -> datetime | None:
    """ISO timestamp ИЛИ eBay-формат 'May 3, 2026 at 4:23 PM' → tz-aware datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_date(v: Any, *, year_hint: int | None = None) -> date | None:
    """ISO 'YYYY-MM-DD' или 'Thu, May 7' / 'May 7' (+year_hint) → date.

    Если year_hint не задан и в строке нет года — берётся текущий год.
    """
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS_WITH_YEAR:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    year = year_hint or datetime.now(timezone.utc).year
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            partial = datetime.strptime(s, fmt)
            return partial.replace(year=year).date()
        except ValueError:
            continue
    return None


# ─── Сериализация результатов для tool-сообщений ────────────────────────────

def _jsonify(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    return value


def to_tool_content(result: Any) -> str:
    return json.dumps(_jsonify(result), ensure_ascii=False, default=str)


# ─── execute_sql ────────────────────────────────────────────────────────────

async def execute_sql(pool: asyncpg.Pool, query: str) -> dict:
    """Один SQL в отдельной транзакции с source='user_chat'.

    Всегда fetch: rows = всё, что вернёт запрос (включая RETURNING). Без угадайки
    по первому слову. Если модель хочет увидеть результат UPDATE/DELETE — добавит
    RETURNING (см. промпт).
    """
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.source = 'user_chat'")
                rows = await conn.fetch(query)
            return {
                "rows": [_jsonify(dict(r)) for r in rows],
                "rowcount": len(rows),
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}


# ─── execute_save_order ─────────────────────────────────────────────────────

_UPSERT_ORDER_SQL = """
INSERT INTO orders(
    order_number, sold_by, ordered_at, order_total_usd,
    item_subtotal_usd, shipping_usd, sales_tax_usd,
    delivery_status, delivered_date, arriving_by_date,
    shipping_service, is_untracked
) VALUES (
    $1, $2, $3::timestamptz, $4::numeric,
    $5::numeric, $6::numeric, $7::numeric,
    $8, $9::date, $10,
    $11, COALESCE($12, false)
)
ON CONFLICT (order_number) DO UPDATE SET
    sold_by           = EXCLUDED.sold_by,
    ordered_at        = COALESCE(EXCLUDED.ordered_at,        orders.ordered_at),
    order_total_usd   = EXCLUDED.order_total_usd,
    item_subtotal_usd = COALESCE(EXCLUDED.item_subtotal_usd, orders.item_subtotal_usd),
    shipping_usd      = COALESCE(EXCLUDED.shipping_usd,      orders.shipping_usd),
    sales_tax_usd     = COALESCE(EXCLUDED.sales_tax_usd,     orders.sales_tax_usd),
    delivery_status   = COALESCE(EXCLUDED.delivery_status,   orders.delivery_status),
    delivered_date    = COALESCE(EXCLUDED.delivered_date,    orders.delivered_date),
    arriving_by_date  = COALESCE(EXCLUDED.arriving_by_date,  orders.arriving_by_date),
    shipping_service  = COALESCE(EXCLUDED.shipping_service,  orders.shipping_service),
    is_untracked      = COALESCE($12, orders.is_untracked)
RETURNING order_id;
"""

_UPSERT_ITEM_SQL = """
INSERT INTO order_items(order_id, item_number, item_title, item_quantity, item_line_total_usd)
VALUES ($1, $2, $3, $4, $5::numeric)
ON CONFLICT (order_id, item_number) DO UPDATE SET
    item_title          = EXCLUDED.item_title,
    item_quantity       = EXCLUDED.item_quantity,
    item_line_total_usd = EXCLUDED.item_line_total_usd;
"""

_INSERT_REFUND_SQL = """
INSERT INTO order_refunds(order_id, refund_amount_usd, refund_date, refund_note)
VALUES ($1, $2::numeric, $3::date, $4);
"""

_UPSERT_TRACKING_SQL = """
INSERT INTO order_tracking_numbers(order_id, tracking_number)
VALUES ($1, $2)
ON CONFLICT DO NOTHING;
"""

_LINK_SCREENSHOTS_SQL = """
WITH upd AS (
    UPDATE screenshots
       SET order_id = $1, agent_status = 'done', last_error = NULL
     WHERE sha256 = ANY($2::bytea[])
    RETURNING 1
)
SELECT count(*) FROM upd;
"""


async def execute_save_order(pool: asyncpg.Pool, args: dict) -> dict:
    try:
        sha_bytes = [bytes.fromhex(s) for s in args.get("screenshot_shas") or []]
    except ValueError as e:
        return {"error": f"bad sha256 in screenshot_shas: {e}"}
    if not sha_bytes:
        return {"error": "screenshot_shas is empty"}
    if not (args.get("items") or []):
        return {"error": "items is empty"}

    ordered_at = parse_timestamp(args.get("ordered_at"))
    delivered = parse_date(
        args.get("delivered_date"),
        year_hint=ordered_at.year if ordered_at else None,
    )
    order_total = parse_money(args.get("order_total_usd"))
    if order_total is None:
        return {"error": "order_total_usd не распознан"}

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.source = 'screenshot'")

                # все sha должны существовать в screenshots
                found = await conn.fetchval(
                    "SELECT count(*) FROM screenshots WHERE sha256 = ANY($1::bytea[])",
                    sha_bytes,
                )
                if found != len(sha_bytes):
                    return {"error": f"some screenshot sha256 not found ({found}/{len(sha_bytes)})"}

                # снимки не должны быть привязаны к ДРУГИМ заказам
                conflicts = await conn.fetch(
                    "SELECT encode(sha256,'hex') AS sha, order_id FROM screenshots "
                    "WHERE sha256 = ANY($1::bytea[]) AND order_id IS NOT NULL",
                    sha_bytes,
                )

                order_id = await conn.fetchval(
                    _UPSERT_ORDER_SQL,
                    args["order_number"],
                    args["sold_by"],
                    ordered_at,
                    order_total,
                    parse_money(args.get("item_subtotal_usd")),
                    parse_money(args.get("shipping_usd")),
                    parse_money(args.get("sales_tax_usd")),
                    args.get("delivery_status"),
                    delivered,
                    args.get("arriving_by_date"),
                    args.get("shipping_service"),
                    args.get("is_untracked"),
                )

                # заблокировать конфликт со снимком, привязанным к ДРУГОМУ заказу
                bad = [r for r in conflicts if r["order_id"] != order_id]
                if bad:
                    return {"error": "screenshots already linked to other orders: "
                                     + ", ".join(r["sha"][:12] for r in bad)}

                for it in args["items"]:
                    line_total = parse_money(it.get("item_line_total_usd"))
                    if line_total is None:
                        return {"error": f"item_line_total_usd не распознан "
                                         f"для {it.get('item_number')}"}
                    await conn.execute(
                        _UPSERT_ITEM_SQL,
                        order_id,
                        it["item_number"],
                        it["item_title"],
                        it["item_quantity"],
                        line_total,
                    )

                for rf in (args.get("refunds") or []):
                    refund_amount = parse_money(rf.get("refund_amount_usd"))
                    if refund_amount is None:
                        continue
                    refund_date = parse_date(
                        rf.get("refund_date"),
                        year_hint=ordered_at.year if ordered_at else None,
                    )
                    await conn.execute(
                        _INSERT_REFUND_SQL,
                        order_id,
                        refund_amount,
                        refund_date,
                        rf.get("refund_note"),
                    )

                for tn in (args.get("tracking_numbers") or []):
                    await conn.execute(_UPSERT_TRACKING_SQL, order_id, tn)

                linked = await conn.fetchval(_LINK_SCREENSHOTS_SQL, order_id, sha_bytes)

                return {
                    "ok": True,
                    "order_id": order_id,
                    "order_number": args["order_number"],
                    "linked_screenshots": int(linked or 0),
                }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
