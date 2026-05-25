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

from .matching import run_matching
from .validation import bad_tracking_reason, is_valid_order_number, normalize_tracking


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
_VALIDATION_MONEY_TOLERANCE = Decimal("0.05")
_FOREIGN_CURRENCY_RE = re.compile(
    r"(?i)(\bCAD\b|\bGBP\b|\bEUR\b|C\s*\$|£|€)"
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


def _has_value(v: Any) -> bool:
    return v is not None and (not isinstance(v, str) or bool(v.strip()))


def _clean_str(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _norm_text(v: Any) -> str:
    return re.sub(r"\s+", " ", _clean_str(v)).casefold()


def _norm_tracking(v: Any) -> str:
    return normalize_tracking(_clean_str(v))


def _short_shas(shas: list[str]) -> str:
    return ", ".join(s[:12] for s in shas)


def validation_error(code: str, message: str, **context: Any) -> dict:
    out = {"error": code, "message": message}
    out.update(context)
    return out


def _parse_required_money(field: str, value: Any) -> Decimal | dict:
    if isinstance(value, str) and _FOREIGN_CURRENCY_RE.search(value):
        return validation_error(
            "foreign_currency_not_converted",
            f"{field}: видна иностранная валюта, передай уже проверенное USD-значение",
            field=field,
            value=value,
        )
    money = parse_money(value)
    if money is None:
        return validation_error(
            "money_parse_error",
            f"{field} не распознан: {value!r}",
            field=field,
            value=value,
        )
    return money


def _parse_optional_money(field: str, value: Any) -> Decimal | dict | None:
    if not _has_value(value):
        return None
    return _parse_required_money(field, value)


def _parse_optional_timestamp(field: str, value: Any) -> datetime | dict | None:
    if not _has_value(value):
        return None
    parsed = parse_timestamp(value)
    if parsed is None:
        return validation_error(
            "date_parse_error",
            f"{field} не распознан: {value!r}",
            field=field,
            value=value,
        )
    return parsed


def _parse_optional_date(field: str, value: Any, *, year_hint: int | None) -> date | dict | None:
    if not _has_value(value):
        return None
    parsed = parse_date(value, year_hint=year_hint)
    if parsed is None:
        return validation_error(
            "date_parse_error",
            f"{field} не распознан: {value!r}",
            field=field,
            value=value,
        )
    return parsed


def _dedupe_refunds(refunds: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for rf in refunds:
        key = (
            str(rf["refund_amount_usd"]),
            rf["refund_date"].isoformat() if rf["refund_date"] else "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(rf)
    return out


def parse_save_order_args(args: dict) -> dict:
    screenshot_shas = [_clean_str(s) for s in (args.get("screenshot_shas") or [])]
    try:
        sha_bytes = [bytes.fromhex(s) for s in screenshot_shas]
    except ValueError as e:
        return validation_error("bad_sha256", f"bad sha256 in screenshot_shas: {e}")
    if not sha_bytes:
        return validation_error("empty_screenshot_shas", "screenshot_shas is empty")

    def fail(code: str, message: str, **context: Any) -> dict:
        return validation_error(
            code,
            message,
            screenshot_shas=screenshot_shas,
            sha_bytes=sha_bytes,
            **context,
        )

    order_number = _clean_str(args.get("order_number"))
    sold_by = _clean_str(args.get("sold_by"))
    if not order_number:
        return fail("missing_order_number", "order_number is empty")
    if not is_valid_order_number(order_number):
        return fail(
            "bad_order_number_format",
            f"order_number {order_number!r} не похож на eBay-формат NN-NNNNN-NNNNN",
        )
    if not sold_by:
        return fail("missing_sold_by", "sold_by is empty")

    ordered_at = _parse_optional_timestamp("ordered_at", args.get("ordered_at"))
    if isinstance(ordered_at, dict):
        return fail(ordered_at["error"], ordered_at["message"], **{
            k: v for k, v in ordered_at.items() if k not in ("error", "message")
        })
    delivered_date = _parse_optional_date(
        "delivered_date",
        args.get("delivered_date"),
        year_hint=ordered_at.year if ordered_at else None,
    )
    if isinstance(delivered_date, dict):
        return fail(delivered_date["error"], delivered_date["message"], **{
            k: v for k, v in delivered_date.items() if k not in ("error", "message")
        })

    order_total = _parse_required_money("order_total_usd", args.get("order_total_usd"))
    if isinstance(order_total, dict):
        return fail(order_total["error"], order_total["message"], **{
            k: v for k, v in order_total.items() if k not in ("error", "message")
        })
    item_subtotal = _parse_optional_money("item_subtotal_usd", args.get("item_subtotal_usd"))
    if isinstance(item_subtotal, dict):
        return fail(item_subtotal["error"], item_subtotal["message"], **{
            k: v for k, v in item_subtotal.items() if k not in ("error", "message")
        })
    shipping = _parse_optional_money("shipping_usd", args.get("shipping_usd"))
    if isinstance(shipping, dict):
        return fail(shipping["error"], shipping["message"], **{
            k: v for k, v in shipping.items() if k not in ("error", "message")
        })
    sales_tax = _parse_optional_money("sales_tax_usd", args.get("sales_tax_usd"))
    if isinstance(sales_tax, dict):
        return fail(sales_tax["error"], sales_tax["message"], **{
            k: v for k, v in sales_tax.items() if k not in ("error", "message")
        })

    items_in = args.get("items") or []
    if not items_in:
        return fail("empty_items", "items is empty")
    items: list[dict] = []
    for i, it in enumerate(items_in, start=1):
        item_number = _clean_str(it.get("item_number"))
        item_title = _clean_str(it.get("item_title"))
        quantity = it.get("item_quantity")
        if not item_number:
            return fail("missing_item_number", f"item #{i}: item_number is empty")
        if not item_title:
            return fail("missing_item_title", f"item {item_number}: item_title is empty")
        if not isinstance(quantity, int):
            return fail(
                "bad_item_quantity",
                f"item {item_number}: item_quantity must be an integer",
                item_number=item_number,
                value=quantity,
            )
        line_total = _parse_required_money(
            "items.item_line_total_usd",
            it.get("item_line_total_usd"),
        )
        if isinstance(line_total, dict):
            line_total["item_number"] = item_number
            return fail(line_total["error"], line_total["message"], **{
                k: v for k, v in line_total.items() if k not in ("error", "message")
            })
        items.append({
            "item_number": item_number,
            "item_title": item_title,
            "item_quantity": quantity,
            "item_line_total_usd": line_total,
        })

    refunds: list[dict] = []
    for i, rf in enumerate(args.get("refunds") or [], start=1):
        amount = _parse_required_money("refunds.refund_amount_usd", rf.get("refund_amount_usd"))
        if isinstance(amount, dict):
            amount["refund_index"] = i
            return fail(amount["error"], amount["message"], **{
                k: v for k, v in amount.items() if k not in ("error", "message")
            })
        refund_date = _parse_optional_date(
            "refunds.refund_date",
            rf.get("refund_date"),
            year_hint=ordered_at.year if ordered_at else None,
        )
        if isinstance(refund_date, dict):
            refund_date["refund_index"] = i
            return fail(refund_date["error"], refund_date["message"], **{
                k: v for k, v in refund_date.items() if k not in ("error", "message")
            })
        refunds.append({
            "refund_amount_usd": amount,
            "refund_date": refund_date,
            "refund_note": rf.get("refund_note"),
        })

    tracking_numbers = [
        _clean_str(t) for t in (args.get("tracking_numbers") or [])
        if _clean_str(t)
    ]
    for tn in tracking_numbers:
        reason = bad_tracking_reason(tn)
        if reason:
            return fail("bad_tracking_format", reason)

    return {
        "screenshot_shas": screenshot_shas,
        "sha_bytes": sha_bytes,
        "order_number": order_number,
        "sold_by": sold_by,
        "ordered_at": ordered_at,
        "order_total_usd": order_total,
        "item_subtotal_usd": item_subtotal,
        "shipping_usd": shipping,
        "sales_tax_usd": sales_tax,
        "delivery_status": args.get("delivery_status"),
        "delivered_date": delivered_date,
        "arriving_by_date": args.get("arriving_by_date"),
        "shipping_service": args.get("shipping_service"),
        "is_untracked": args.get("is_untracked"),
        "items": items,
        "refunds": _dedupe_refunds(refunds),
        "tracking_numbers": tracking_numbers,
    }


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

# Листинг (item_number) — отдельная сущность с каноническим титулом. Титул берём
# первый и не перезаписываем (у дублей он идентичен).
_UPSERT_LISTING_SQL = """
INSERT INTO items(item_number, item_title)
VALUES ($1, $2)
ON CONFLICT (item_number) DO NOTHING;
"""

_UPSERT_ITEM_SQL = """
INSERT INTO order_items(order_id, item_number, item_quantity, item_line_total_usd)
VALUES ($1, $2, $3, $4::numeric)
ON CONFLICT (order_id, item_number) DO UPDATE SET
    item_quantity       = EXCLUDED.item_quantity,
    item_line_total_usd = EXCLUDED.item_line_total_usd;
"""

_INSERT_REFUND_SQL = """
INSERT INTO order_refunds(order_id, refund_amount_usd, refund_date, refund_note)
SELECT $1, $2::numeric, $3::date, $4
WHERE NOT EXISTS (
    SELECT 1
      FROM order_refunds
     WHERE order_id = $1
       AND refund_amount_usd = $2::numeric
       AND refund_date IS NOT DISTINCT FROM $3::date
       AND COALESCE(refund_note, '') = COALESCE($4, '')
);
"""

_UPSERT_TRACKING_SQL = """
INSERT INTO order_tracking_numbers(order_id, tracking_number, source)
VALUES ($1, $2, 'ebay')
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

_MARK_SCREENSHOTS_FAILED_SQL = """
UPDATE screenshots
   SET agent_status = 'failed', last_error = $2
 WHERE sha256 = ANY($1::bytea[]);
"""


async def mark_screenshots_failed(
    executor: asyncpg.Connection | asyncpg.Pool,
    sha_bytes: list[bytes],
    message: str,
) -> None:
    if not sha_bytes:
        return
    msg = message[:1000]
    await executor.execute(_MARK_SCREENSHOTS_FAILED_SQL, sha_bytes, msg)


async def validate_order_before_save(conn: asyncpg.Connection, parsed: dict) -> dict | None:
    sha_bytes = parsed["sha_bytes"]
    shas = parsed["screenshot_shas"]

    rows = await conn.fetch(
        """
        SELECT
            encode(s.sha256, 'hex') AS sha,
            s.ocr_status::text AS ocr_status,
            s.agent_status::text AS agent_status,
            s.order_id,
            r.raw_json
        FROM screenshots s
        LEFT JOIN raw_ocr r ON r.sha256 = s.sha256
        WHERE s.sha256 = ANY($1::bytea[])
        """,
        sha_bytes,
    )
    if len(rows) != len(sha_bytes):
        found = {r["sha"] for r in rows}
        missing = [s for s in shas if s not in found]
        return validation_error(
            "screenshot_not_found",
            f"some screenshot sha256 not found ({len(rows)}/{len(sha_bytes)}): {_short_shas(missing)}",
            screenshot_shas=shas,
            missing_shas=missing,
        )

    for r in rows:
        if r["ocr_status"] != "done":
            return validation_error(
                "screenshot_ocr_not_done",
                f"скриншот {r['sha'][:12]} имеет ocr_status={r['ocr_status']}, нужен done",
                screenshot_shas=shas,
            )
        if r["agent_status"] != "pending":
            return validation_error(
                "screenshot_not_pending",
                f"скриншот {r['sha'][:12]} имеет agent_status={r['agent_status']}, нужен pending",
                screenshot_shas=shas,
            )
        if r["raw_json"] is None:
            return validation_error(
                "raw_ocr_missing",
                f"для скриншота {r['sha'][:12]} нет raw_ocr",
                screenshot_shas=shas,
            )
        if r["raw_json"].get("is_order_details") is not True:
            return validation_error(
                "not_order_details",
                f"скриншот {r['sha'][:12]} не распознан как eBay Order details",
                screenshot_shas=shas,
            )
        if r["order_id"] is not None:
            return validation_error(
                "screenshot_already_linked",
                f"скриншот {r['sha'][:12]} уже привязан к order_id={r['order_id']}",
                screenshot_shas=shas,
                order_id=r["order_id"],
            )

    existing = await conn.fetchrow(
        """
        SELECT order_id, order_number, sold_by, order_total_usd
          FROM orders
         WHERE order_number = $1
        """,
        parsed["order_number"],
    )
    if existing is not None:
        if _norm_text(existing["sold_by"]) != _norm_text(parsed["sold_by"]):
            return validation_error(
                "order_seller_conflict",
                f"заказ {parsed['order_number']} уже сохранён с seller {existing['sold_by']!r}, новый seller {parsed['sold_by']!r}",
                screenshot_shas=shas,
                existing_order_id=existing["order_id"],
            )
        if abs(Decimal(existing["order_total_usd"]) - parsed["order_total_usd"]) > _VALIDATION_MONEY_TOLERANCE:
            return validation_error(
                "order_total_conflict",
                f"заказ {parsed['order_number']} уже сохранён с total ${existing['order_total_usd']}, новый total ${parsed['order_total_usd']}",
                screenshot_shas=shas,
                existing_order_id=existing["order_id"],
            )

        existing_items = await conn.fetch(
            """
            SELECT item_number, item_quantity, item_line_total_usd
              FROM order_items
             WHERE order_id = $1
               AND item_number = ANY($2::text[])
            """,
            existing["order_id"],
            [it["item_number"] for it in parsed["items"]],
        )
        by_item = {r["item_number"]: r for r in existing_items}
        for it in parsed["items"]:
            old = by_item.get(it["item_number"])
            if old is None:
                continue
            if old["item_quantity"] != it["item_quantity"]:
                return validation_error(
                    "item_quantity_conflict",
                    f"заказ {parsed['order_number']}, item {it['item_number']}: quantity уже {old['item_quantity']}, новый {it['item_quantity']}",
                    screenshot_shas=shas,
                    existing_order_id=existing["order_id"],
                    item_number=it["item_number"],
                )
            if abs(Decimal(old["item_line_total_usd"]) - it["item_line_total_usd"]) > _VALIDATION_MONEY_TOLERANCE:
                return validation_error(
                    "item_line_total_conflict",
                    f"заказ {parsed['order_number']}, item {it['item_number']}: line total уже ${old['item_line_total_usd']}, новый ${it['item_line_total_usd']}",
                    screenshot_shas=shas,
                    existing_order_id=existing["order_id"],
                    item_number=it["item_number"],
                )

    norm_tracks = sorted({
        _norm_tracking(t) for t in parsed["tracking_numbers"]
        if _norm_tracking(t)
    })
    if norm_tracks:
        track_rows = await conn.fetch(
            """
            SELECT
                o.order_id,
                o.order_number,
                o.sold_by,
                t.tracking_number,
                upper(regexp_replace(t.tracking_number, '[^A-Za-z0-9]', '', 'g')) AS norm_tracking
            FROM order_tracking_numbers t
            JOIN orders o ON o.order_id = t.order_id
            WHERE upper(regexp_replace(t.tracking_number, '[^A-Za-z0-9]', '', 'g')) = ANY($1::text[])
            """,
            norm_tracks,
        )
        for r in track_rows:
            if _norm_text(r["sold_by"]) != _norm_text(parsed["sold_by"]):
                return validation_error(
                    "tracking_seller_conflict",
                    f"трек {r['tracking_number']} уже есть у заказа {r['order_number']} от seller {r['sold_by']!r}, новый seller {parsed['sold_by']!r}",
                    screenshot_shas=shas,
                    existing_order_id=r["order_id"],
                    existing_order_number=r["order_number"],
                    tracking_number=r["tracking_number"],
                )
    return None


async def execute_save_order(pool: asyncpg.Pool, args: dict) -> dict:
    parsed = parse_save_order_args(args)
    if "error" in parsed:
        sha_bytes = parsed.get("sha_bytes") or []
        if sha_bytes:
            async with pool.acquire() as conn:
                await mark_screenshots_failed(conn, sha_bytes, parsed["message"])
        out = dict(parsed)
        out.pop("sha_bytes", None)
        return out

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.source = 'screenshot'")

                err = await validate_order_before_save(conn, parsed)
                if err is not None:
                    await mark_screenshots_failed(conn, parsed["sha_bytes"], err["message"])
                    return err

                order_id = await conn.fetchval(
                    _UPSERT_ORDER_SQL,
                    parsed["order_number"],
                    parsed["sold_by"],
                    parsed["ordered_at"],
                    parsed["order_total_usd"],
                    parsed["item_subtotal_usd"],
                    parsed["shipping_usd"],
                    parsed["sales_tax_usd"],
                    parsed["delivery_status"],
                    parsed["delivered_date"],
                    parsed["arriving_by_date"],
                    parsed["shipping_service"],
                    parsed["is_untracked"],
                )

                for it in parsed["items"]:
                    await conn.execute(_UPSERT_LISTING_SQL, it["item_number"], it["item_title"])
                    await conn.execute(
                        _UPSERT_ITEM_SQL,
                        order_id,
                        it["item_number"],
                        it["item_quantity"],
                        it["item_line_total_usd"],
                    )

                for rf in parsed["refunds"]:
                    await conn.execute(
                        _INSERT_REFUND_SQL,
                        order_id,
                        rf["refund_amount_usd"],
                        rf["refund_date"],
                        rf.get("refund_note"),
                    )

                for tn in parsed["tracking_numbers"]:
                    await conn.execute(_UPSERT_TRACKING_SQL, order_id, tn)

                linked = await conn.fetchval(_LINK_SCREENSHOTS_SQL, order_id, parsed["sha_bytes"])
                result = {
                    "ok": True,
                    "order_id": order_id,
                    "order_number": parsed["order_number"],
                    "linked_screenshots": int(linked or 0),
                }
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            await mark_screenshots_failed(pool, parsed["sha_bytes"], message)
            return validation_error(
                "db_invariant_error",
                message,
                screenshot_shas=parsed["screenshot_shas"],
            )

    # Заказ сохранён и закоммичен. Матчинг item -> деталь каталога — ОТДЕЛЬНЫМ
    # best-effort шагом: недоступность каталога/FDW не должна ронять сохранение.
    # Матчатся только pending-листинги; уже разобранные переиспользуются.
    item_numbers = [it["item_number"] for it in parsed["items"]]
    try:
        result["matches"] = await run_matching(pool, item_numbers)
    except Exception as e:
        result["matches"] = []
        result["match_deferred"] = f"{type(e).__name__}: {e}"
    return result
