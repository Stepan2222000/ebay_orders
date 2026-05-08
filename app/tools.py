"""Инструменты стадии B: sql и save_order.

OPENAI_TOOLS — спецификация для tool-calling.
execute_sql / execute_save_order — серверные обработчики.
Источник изменений (`SET LOCAL app.source = ...`) выставляется на уровне инструмента,
аудит-триггер из миграции 001 подхватит его автоматически.
"""
import json
from datetime import date, datetime
from decimal import Decimal
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
                        "description": "ISO timestamp 'YYYY-MM-DD HH:MM:SS' (можно с TZ), "
                                       "если уверен в формате; иначе null.",
                    },
                    "order_total_usd": {"type": "number"},
                    "item_subtotal_usd": {"type": ["number", "null"]},
                    "shipping_usd":      {"type": ["number", "null"]},
                    "sales_tax_usd":     {"type": ["number", "null"]},
                    "delivery_status":   {"type": ["string", "null"]},
                    "delivered_date": {
                        "type": ["string", "null"],
                        "description": "Дата 'YYYY-MM-DD' если уверен; иначе null.",
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
                                "item_line_total_usd": {"type": "number"},
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
                                "refund_amount_usd": {"type": "number"},
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
                    args.get("ordered_at"),
                    args["order_total_usd"],
                    args.get("item_subtotal_usd"),
                    args.get("shipping_usd"),
                    args.get("sales_tax_usd"),
                    args.get("delivery_status"),
                    args.get("delivered_date"),
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
                    await conn.execute(
                        _UPSERT_ITEM_SQL,
                        order_id,
                        it["item_number"],
                        it["item_title"],
                        it["item_quantity"],
                        it["item_line_total_usd"],
                    )

                for rf in (args.get("refunds") or []):
                    await conn.execute(
                        _INSERT_REFUND_SQL,
                        order_id,
                        rf["refund_amount_usd"],
                        rf.get("refund_date"),
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
