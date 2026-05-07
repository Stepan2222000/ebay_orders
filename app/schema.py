"""JSON-схема сырого распознавания (формат сырого распознавания из SPEC).

Описывает только поведение модели на одном снимке. Поле sha256 не входит:
оно есть в БД как PK и подставляется бэкендом.
"""

_NULLABLE_STR = {"type": ["string", "null"]}

_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_number", "item_title", "item_quantity", "item_line_total_text"],
    "properties": {
        "item_number": _NULLABLE_STR,
        "item_title": _NULLABLE_STR,
        "item_quantity": {"type": ["integer", "null"]},
        "item_line_total_text": _NULLABLE_STR,
    },
}

_REFUND = {
    "type": "object",
    "additionalProperties": False,
    "required": ["refund_amount_text", "refund_date", "refund_note"],
    "properties": {
        "refund_amount_text": _NULLABLE_STR,
        "refund_date": _NULLABLE_STR,
        "refund_note": _NULLABLE_STR,
    },
}

RAW_OCR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_order_details", "visible_text", "observed", "unreadable"],
    "properties": {
        "is_order_details": {"type": ["boolean", "null"]},
        "visible_text": {"type": "string"},
        "observed": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "order_number", "ordered_at", "sold_by",
                "order_total_text", "item_subtotal_text",
                "shipping_text", "sales_tax_text",
                "delivery_status", "delivered_date", "arriving_by_date",
                "shipping_service", "tracking_numbers",
                "items", "refunds",
            ],
            "properties": {
                "order_number": _NULLABLE_STR,
                "ordered_at": _NULLABLE_STR,
                "sold_by": _NULLABLE_STR,
                "order_total_text": _NULLABLE_STR,
                "item_subtotal_text": _NULLABLE_STR,
                "shipping_text": _NULLABLE_STR,
                "sales_tax_text": _NULLABLE_STR,
                "delivery_status": _NULLABLE_STR,
                "delivered_date": _NULLABLE_STR,
                "arriving_by_date": _NULLABLE_STR,
                "shipping_service": _NULLABLE_STR,
                "tracking_numbers": {"type": "array", "items": {"type": "string"}},
                "items": {"type": "array", "items": _ITEM},
                "refunds": {"type": "array", "items": _REFUND},
            },
        },
        "unreadable": {"type": "array", "items": {"type": "string"}},
    },
}
