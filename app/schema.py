from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ItemObs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_number: str | None = Field(
        description="Внутренний eBay item id (обычно ~12 цифр), если виден."
    )
    item_title: str | None = Field(
        description="Полный заголовок позиции, без сокращений."
    )
    item_quantity_text: str | None = Field(
        description="Количество как сырой текст ровно так, как видно."
    )
    item_line_total_text: str | None = Field(
        description="Итог по строке позиции (цена × количество) с валютой, как видно."
    )


class RefundObs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refund_amount_text: str | None = Field(
        description="Сумма возврата с валютой как сырой текст."
    )
    refund_date_text: str | None = Field(
        description="Дата возврата как сырой текст."
    )
    refund_note: str | None = Field(
        description="Короткая подпись или причина рядом с возвратом, если есть."
    )


class Observed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_number: str | None = Field(
        description=(
            "Номер заказа eBay в формате NN-NNNNN-NNNNN (например, "
            "12-12345-67890). Обычно подписан «Order number» в шапке деталей."
        )
    )
    ordered_at_text: str | None = Field(
        description=(
            "Дата размещения заказа («Order placed», «Order date», «Ordered "
            "on», «Time placed») как сырой текст."
        )
    )
    sold_by: str | None = Field(
        description="Username продавца ровно так, как написано в блоке «Sold by»."
    )
    order_total_text: str | None = Field(
        description="Строка «Order total» с валютой, как видно."
    )
    item_subtotal_text: str | None = Field(
        description=(
            "Строка «Item subtotal» / «Subtotal» с валютой — сумма позиций "
            "до доставки и налога."
        )
    )
    shipping_text: str | None = Field(
        description="Строка «Shipping» (например, '$9.95' или 'Free')."
    )
    sales_tax_text: str | None = Field(
        description="Строка «Sales tax» / «Tax» с валютой."
    )
    delivery_status: str | None = Field(
        description=(
            "Короткий статус доставки одной фразой («Delivered», «Arriving», "
            "«Shipped», «In transit», «Cancelled», «Refunded»). Без дат — "
            "даты идут в delivered_date_text и arriving_by_date."
        )
    )
    delivered_date_text: str | None = Field(
        description=(
            "Фактическая дата доставки, если заказ доставлен. Иначе null."
        )
    )
    arriving_by_date: str | None = Field(
        description=(
            "Ожидаемая дата прибытия для ещё не доставленных заказов "
            "(«Arriving by …», «Estimated delivery …»). Для уже доставленных — null."
        )
    )
    shipping_service: str | None = Field(
        description=(
            "Название перевозчика или сервиса как написано на странице "
            "(«USPS Ground Advantage», «FedEx Home Delivery», «UPS Ground»)."
        )
    )
    tracking_numbers: list[str] = Field(
        description=(
            "Массив всех видимых трек-номеров: только сами номера, без "
            "меток перевозчика, без повторов."
        )
    )
    items: list[ItemObs] = Field(
        description="Массив позиций заказа, по одной записи на каждую позицию."
    )
    refunds: list[RefundObs] = Field(
        description="Массив возвратов, по одной записи на каждый отдельный возврат."
    )


class RawOcrPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_order_details: bool | None = Field(
        description="true, если на скриншоте — страница деталей одного eBay-заказа; иначе false."
    )
    visible_text: str = Field(
        description=(
            "Весь читаемый текст страницы целиком, в естественном порядке "
            "чтения. Если текста нет — пустая строка."
        )
    )
    observed: Observed
    unreadable: list[str] = Field(
        description=(
            "Список коротких заметок о фрагментах, которые реально "
            "присутствуют на пикселях, но не получилось разобрать (блюр, "
            "блик, обрезано). Не перечисляй здесь поля, которых на "
            "скриншоте просто нет — их значение должно быть null."
        )
    )


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
