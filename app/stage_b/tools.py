"""Tool definitions for Stage B agent (OpenAI/OpenRouter function-calling).

The schemas mirror SPEC.yaml [[инструмент чтения базы]],
[[инструмент сохранения заказа]] and [[инструмент мягкого удаления заказа]]
exactly. Fields from [[поля которые не сохраняются]] are physically
absent from save_order_details so the model cannot pass them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------- Pydantic input shapes (also used by dispatch.py) ----------


class SqlReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(description="A single SELECT statement.")


class SaveOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_number: str
    item_title: str
    item_quantity: int | None = Field(
        default=None,
        description="Quantity if visible. If unknown/null, server stores 1.",
    )
    item_line_total_text: str = Field(
        description="Raw text from raw_ocr, e.g. '$49.00'. Server parses it.",
    )


class SaveOrderRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refund_amount_text: str = Field(description="Raw text, e.g. '$3.06'.")
    refund_date_text: str | None = Field(default=None, description="Raw date text or null.")
    refund_note: str | None = None


class SaveOrderDetailsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: str = Field(description="eBay order id, e.g. 12-12345-67890.")
    sold_by: str = Field(description="Seller username from the 'Sold by' block.")
    order_total_text: str = Field(description="Raw 'Order total' text, e.g. '$49.00'.")
    item_subtotal_text: str | None = None
    shipping_text: str | None = Field(
        default=None,
        description="Raw shipping text. 'Free' → 0.00 server-side.",
    )
    sales_tax_text: str | None = None
    delivery_status: str | None = None
    delivered_date_text: str | None = None
    arriving_by_date: str | None = None
    shipping_service: str | None = None
    tracking_numbers: list[str] | None = Field(
        default_factory=list, description="All visible tracking numbers, no duplicates."
    )
    is_untracked: bool | None = None
    items: list[SaveOrderItem] = Field(default_factory=list)
    refunds: list[SaveOrderRefund] = Field(default_factory=list)
    source_sha256s: list[str] = Field(
        description="sha256s of screenshots that this order is built from. "
        "All must be agent_status='pending' on the server.",
    )
    source: Literal["screenshot", "user_chat"] = Field(
        description="Where the change came from."
    )


class DeleteOrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: str
    reason: str = Field(description="Short reason for the audit log.")
    requested_text: str = Field(
        description="Verbatim fragment of the user's message that requested the deletion."
    )


class NoConsolidationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256s: list[str]
    reason: str


# ---------- Strict schema export, same trick as Stage A ----------

from app.schema import to_strict_schema  # noqa: E402  (avoid circular)


def _tool(name: str, description: str, model: type[BaseModel]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": to_strict_schema(model),
        },
    }


SQL_READ_TOOL = _tool(
    "sql_read",
    "Run ONE read-only SELECT against the orders database. Allowed tables: "
    "orders, order_items, order_refunds, order_screenshot_links, "
    "order_change_log. Use pg_trgm.similarity(a,b) for fuzzy match on "
    "sold_by/item_title (>= 0.4 is a sane threshold). For delivered-order "
    "questions, delivery_status is free text; use delivered_date IS NOT NULL "
    "OR delivery_status ILIKE '%delivered%'.",
    SqlReadArgs,
)

SAVE_ORDER_DETAILS_TOOL = _tool(
    "save_order_details",
    "Persist a clean order plus its items, refunds, tracking numbers and "
    "screenshot links in one transaction. Money is in USD; the server "
    "parses *_text fields. Upserts by order_number. For screenshot OCR, pass "
    "through every non-null/non-empty observed field exactly: items, refunds, "
    "tracking_numbers, delivery_status, delivered_date_text, shipping_service, "
    "subtotal/shipping/tax when present. Do not save only the required fields.",
    SaveOrderDetailsArgs,
)

DELETE_ORDER_TOOL = _tool(
    "delete_order",
    "Soft-delete an existing order. The server requires `requested_text` "
    "to be a verbatim substring of the user's last message — without that, "
    "the deletion is rejected.",
    DeleteOrderArgs,
)

NO_CONSOLIDATION_TOOL = _tool(
    "no_consolidation",
    "Use when a group of screenshots cannot be consolidated into a clean "
    "order (missing required fields, conflict with DB, ambiguous group).",
    NoConsolidationArgs,
)


def tools_for(branch: Literal["screenshot", "text"]) -> list[dict]:
    if branch == "screenshot":
        return [SQL_READ_TOOL, SAVE_ORDER_DETAILS_TOOL, NO_CONSOLIDATION_TOOL]
    return [SQL_READ_TOOL, SAVE_ORDER_DETAILS_TOOL, DELETE_ORDER_TOOL]
