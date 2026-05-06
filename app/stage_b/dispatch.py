"""Python dispatchers for Stage B tools.

Each function returns a JSON string the model can read back as the
`tool` message content. None of them raise on bad model output: bad args
become `{"error": "..."}` results so the loop can keep going.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import asyncpg
from pglast import parse_sql
from pglast import ast as pgast
from pglast.visitors import Visitor
from pydantic import ValidationError

from app.stage_b.money import MoneyParseError, parse_money_text
from app.stage_b.tools import (
    DeleteOrderArgs,
    SaveOrderDetailsArgs,
    SaveOrderItem,
    SaveOrderRefund,
    SqlReadArgs,
)

log = logging.getLogger("stage_b.dispatch")


_ALLOWED_TABLES = {
    "orders",
    "order_items",
    "order_refunds",
    "order_screenshot_links",
    "order_change_log",
}


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_jsonify)


def _jsonify(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o).__name__)


# ----------------------------------------------------------------------
# sql_read
# ----------------------------------------------------------------------


class _SqlVisitor(Visitor):
    def __init__(self) -> None:
        self.write_stmts: list[str] = []
        self.tables: list[tuple[str | None, str]] = []

    def visit_InsertStmt(self, ancestors, node):  # noqa: N802 — pglast API
        self.write_stmts.append("INSERT")

    def visit_UpdateStmt(self, ancestors, node):  # noqa: N802
        self.write_stmts.append("UPDATE")

    def visit_DeleteStmt(self, ancestors, node):  # noqa: N802
        self.write_stmts.append("DELETE")

    def visit_MergeStmt(self, ancestors, node):  # noqa: N802
        self.write_stmts.append("MERGE")

    def visit_RangeVar(self, ancestors, node):  # noqa: N802
        self.tables.append((node.schemaname, node.relname))


def _validate_select(query: str) -> str | None:
    """Return None if `query` is a single safe SELECT, otherwise an error."""
    try:
        stmts = parse_sql(query)
    except Exception as e:  # noqa: BLE001
        return f"sql parse error: {e}"
    if len(stmts) != 1:
        return "only one statement is allowed"
    top = stmts[0].stmt
    if not isinstance(top, pgast.SelectStmt):
        return "only SELECT is allowed"
    v = _SqlVisitor()
    v(top)
    if v.write_stmts:
        return f"write statements not allowed inside CTEs: {','.join(v.write_stmts)}"
    cte_names: set[str] = set()
    if top.withClause is not None:
        cte_names = {cte.ctename for cte in top.withClause.ctes}
    for schema, rel in v.tables:
        if rel in cte_names and schema is None:
            continue
        if schema not in (None, "public"):
            return f"schema not allowed: {schema}.{rel}"
        if rel.startswith("pg_"):
            return f"system catalog not allowed: {rel}"
        if rel not in _ALLOWED_TABLES:
            return f"table not in allowlist: {rel}"
    return None


async def run_sql_read(pool: asyncpg.Pool, args_raw: dict[str, Any]) -> str:
    try:
        args = SqlReadArgs.model_validate(args_raw)
    except ValidationError as e:
        return _err(f"bad args: {e.errors()}")
    err = _validate_select(args.query)
    if err is not None:
        return _err(err)
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            try:
                rows = await conn.fetch(args.query)
            except Exception as e:  # noqa: BLE001
                return _err(f"{type(e).__name__}: {e}")
    out = [{k: v for k, v in r.items()} for r in rows[:50]]
    return _ok({"rows": out, "row_count": len(rows)})


# ----------------------------------------------------------------------
# save_order_details
# ----------------------------------------------------------------------


def _parse_items(items: list[SaveOrderItem]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        line_total = parse_money_text(it.item_line_total_text)
        if line_total is None:
            raise MoneyParseError(
                f"item {it.item_number}: empty item_line_total_text"
            )
        out.append(
            {
                "item_number": it.item_number,
                "item_title": it.item_title,
                "item_quantity": it.item_quantity or 1,
                "item_line_total_usd": line_total,
            }
        )
    return out


def _parse_refunds(refunds: list[SaveOrderRefund]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in refunds:
        amount = parse_money_text(r.refund_amount_text)
        if amount is None:
            raise MoneyParseError("refund_amount_text is required")
        out.append(
            {
                "refund_amount_usd": amount,
                "refund_date_text": r.refund_date_text,
                "refund_note": r.refund_note,
            }
        )
    return out


async def run_save_order_details(
    pool: asyncpg.Pool, args_raw: dict[str, Any]
) -> str:
    try:
        args = SaveOrderDetailsArgs.model_validate(args_raw)
    except ValidationError as e:
        return _err(f"bad args: {e.errors()}")

    # Money parsing — happens before any DB write so we never half-commit.
    try:
        order_total = parse_money_text(args.order_total_text)
        item_subtotal = parse_money_text(args.item_subtotal_text)
        shipping = parse_money_text(args.shipping_text, allow_free=True)
        sales_tax = parse_money_text(args.sales_tax_text)
        items = _parse_items(args.items)
        refunds = _parse_refunds(args.refunds)
    except MoneyParseError as e:
        return _err(f"money: {e}")

    if order_total is None:
        return _err("order_total_text is required and must be a USD amount")

    # Tracking uniqueness inside the array — we let Postgres enforce too.
    tracking_numbers = args.tracking_numbers or []
    if len(set(tracking_numbers)) != len(tracking_numbers):
        return _err("tracking_numbers contains duplicates")

    # Pre-validation: every source sha256 must currently be a pending
    # screenshot whose raw_ocr says it IS an Order details page.
    if args.source_sha256s:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.sha256,
                       s.agent_status,
                       (r.payload->>'is_order_details')::boolean AS is_od
                  FROM screenshots s
             LEFT JOIN raw_ocr r USING (sha256)
                 WHERE s.sha256 = ANY($1::char(64)[])
                """,
                args.source_sha256s,
            )
        seen = {r["sha256"] for r in rows}
        missing = [s for s in args.source_sha256s if s not in seen]
        if missing:
            return _err(f"unknown source_sha256: {missing[:3]}")
        bad_status = [r["sha256"] for r in rows if r["agent_status"] != "pending"]
        if bad_status:
            return _err(
                f"source_sha256 already consumed (agent_status != pending): "
                f"{bad_status[:3]}"
            )
        not_od = [r["sha256"] for r in rows if r["is_od"] is False]
        if not_od:
            return _err(
                f"source_sha256 is_order_details=false; refusing to link: "
                f"{not_od[:3]}"
            )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL app.source = '{args.source}'")
            await conn.execute(
                """
                INSERT INTO orders (
                    order_number, sold_by,
                    order_total_usd, item_subtotal_usd,
                    shipping_usd, sales_tax_usd,
                    delivery_status, delivered_date,
                    arriving_by_date, shipping_service,
                    tracking_numbers, is_untracked
                ) VALUES (
                    $1, $2,
                    $3, $4,
                    $5, $6,
                    $7, NULLIF($8, '')::date,
                    $9, $10,
                    $11, $12
                )
                ON CONFLICT (order_number) DO UPDATE SET
                  sold_by = COALESCE(NULLIF(EXCLUDED.sold_by,''), orders.sold_by),
                  order_total_usd = EXCLUDED.order_total_usd,
                  item_subtotal_usd = COALESCE(EXCLUDED.item_subtotal_usd, orders.item_subtotal_usd),
                  shipping_usd      = COALESCE(EXCLUDED.shipping_usd,      orders.shipping_usd),
                  sales_tax_usd     = COALESCE(EXCLUDED.sales_tax_usd,     orders.sales_tax_usd),
                  delivery_status   = COALESCE(EXCLUDED.delivery_status,   orders.delivery_status),
                  delivered_date    = COALESCE(EXCLUDED.delivered_date,    orders.delivered_date),
                  arriving_by_date  = COALESCE(EXCLUDED.arriving_by_date,  orders.arriving_by_date),
                  shipping_service  = COALESCE(EXCLUDED.shipping_service,  orders.shipping_service),
                  tracking_numbers  = (
                    SELECT array_agg(DISTINCT x ORDER BY x)
                      FROM unnest(orders.tracking_numbers || EXCLUDED.tracking_numbers) x
                  ),
                  is_untracked      = COALESCE(EXCLUDED.is_untracked,      orders.is_untracked)
                """,
                args.order_number,
                args.sold_by,
                order_total,
                item_subtotal,
                shipping,
                sales_tax,
                args.delivery_status,
                args.delivered_date_text or "",
                args.arriving_by_date,
                args.shipping_service,
                tracking_numbers,
                args.is_untracked,
            )
            for it in items:
                await conn.execute(
                    """
                    INSERT INTO order_items (
                        order_number, item_number, item_title,
                        item_quantity, item_line_total_usd
                    ) VALUES ($1,$2,$3,$4,$5)
                    ON CONFLICT (order_number, item_number) DO UPDATE SET
                      item_title = EXCLUDED.item_title,
                      item_quantity = EXCLUDED.item_quantity,
                      item_line_total_usd = EXCLUDED.item_line_total_usd
                    """,
                    args.order_number,
                    it["item_number"],
                    it["item_title"],
                    it["item_quantity"],
                    it["item_line_total_usd"],
                )
            for r in refunds:
                await conn.execute(
                    """
                    INSERT INTO order_refunds (
                        order_number, refund_amount_usd, refund_date, refund_note
                    ) VALUES ($1, $2, NULLIF($3, '')::date, $4)
                    ON CONFLICT DO NOTHING
                    """,
                    args.order_number,
                    r["refund_amount_usd"],
                    r["refund_date_text"] or "",
                    r["refund_note"],
                )
            for sha in args.source_sha256s:
                await conn.execute(
                    """
                    INSERT INTO order_screenshot_links (order_number, sha256)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    args.order_number,
                    sha,
                )
            if args.source_sha256s:
                await conn.execute(
                    "UPDATE screenshots SET agent_status='done' "
                    "WHERE sha256 = ANY($1::char(64)[])",
                    args.source_sha256s,
                )

    return _ok({"ok": True, "order_number": args.order_number})


# ----------------------------------------------------------------------
# delete_order
# ----------------------------------------------------------------------


async def run_delete_order(
    pool: asyncpg.Pool,
    args_raw: dict[str, Any],
    *,
    last_user_text: str,
) -> str:
    try:
        args = DeleteOrderArgs.model_validate(args_raw)
    except ValidationError as e:
        return _err(f"bad args: {e.errors()}")
    if not args.reason.strip():
        return _err("reason must be non-empty")
    fragment = args.requested_text.strip()
    if not fragment or fragment not in (last_user_text or ""):
        return _err(
            "no explicit user request found: requested_text must be a verbatim "
            "substring of the last user message"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL app.source = 'user_chat'")
            row = await conn.fetchrow(
                """
                UPDATE orders
                   SET deleted_at = now(),
                       deleted_reason = $2
                 WHERE order_number = $1
                   AND deleted_at IS NULL
                RETURNING order_number
                """,
                args.order_number,
                args.reason,
            )
    if row is None:
        return _err(
            f"order {args.order_number} not found or already deleted"
        )
    return _ok({"ok": True, "order_number": args.order_number})
