from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ItemObs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_number: str | None
    item_title: str | None
    item_quantity_text: str | None
    item_line_total_text: str | None


class RefundObs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refund_amount_text: str | None
    refund_date_text: str | None
    refund_note: str | None


class Observed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: str | None
    ordered_at_text: str | None
    sold_by: str | None
    order_total_text: str | None
    item_subtotal_text: str | None
    shipping_text: str | None
    sales_tax_text: str | None
    delivery_status: str | None
    delivered_date_text: str | None
    arriving_by_date: str | None
    shipping_service: str | None
    tracking_numbers: list[str]
    items: list[ItemObs]
    refunds: list[RefundObs]


class RawOcrPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_order_details: bool | None
    visible_text: str
    observed: Observed
    unreadable: list[str]


class UploadResultItem(BaseModel):
    sha256: str
    is_new: bool
    byte_size: int


# OpenAI / OpenRouter strict json_schema requires:
#   - additionalProperties: false on every object
#   - all properties listed in `required`
#   - nullable expressed as ["type", "null"] union, not just optional
# Pydantic produces additionalProperties: false and complete required when
# extra="forbid" and types use `| None` (not `Optional[...] = None`). The only
# adjustment we need is to translate {"anyOf":[{"type":"X"},{"type":"null"}]} to
# the equivalent {"type":["X","null"]} that strict mode accepts.

def _coerce_strict(node: Any) -> Any:
    if isinstance(node, dict):
        if "anyOf" in node and len(node["anyOf"]) == 2:
            a, b = node["anyOf"]
            if a == {"type": "null"} or b == {"type": "null"}:
                non_null = b if a == {"type": "null"} else a
                merged = {k: v for k, v in node.items() if k != "anyOf"}
                non_null = _coerce_strict(non_null)
                if "type" in non_null:
                    merged["type"] = [non_null["type"], "null"]
                    for k, v in non_null.items():
                        if k != "type":
                            merged[k] = v
                    return merged
        return {k: _coerce_strict(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_strict(x) for x in node]
    return node


def to_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    # Inline $defs so that strict-mode validators that don't follow $ref still
    # work with K2.6 / OpenRouter.
    defs = raw.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                name = node["$ref"].split("/")[-1]
                resolved = defs[name]
                # Merge sibling keys (rare but possible).
                merged = {k: v for k, v in node.items() if k != "$ref"}
                for k, v in resolved.items():
                    merged.setdefault(k, v)
                return inline(merged)
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(x) for x in node]
        return node

    inlined = inline(raw)
    return _coerce_strict(inlined)
