from __future__ import annotations

import argparse
import asyncio
import hashlib
import pathlib
import sys

from app.ocr_client import OcrClient
from app.schema import RawOcrPayload


def _print_human(path: pathlib.Path, sha: str, size: int, payload: RawOcrPayload, ms: int) -> None:
    print(f"file: {path}")
    print(f"sha:  {sha[:12]}...{sha[-4:]}")
    print(f"size: {size/1024:.1f} KB")
    print(
        f"status: order_details={payload.is_order_details}  recognition={ms} ms"
    )
    obs = payload.observed
    pairs: list[tuple[str, str | None]] = [
        ("order_number", obs.order_number),
        ("sold_by", obs.sold_by),
        ("ordered_at", obs.ordered_at_text),
        ("order_total", obs.order_total_text),
        ("item_subtotal", obs.item_subtotal_text),
        ("shipping", obs.shipping_text),
        ("sales_tax", obs.sales_tax_text),
        ("delivery_status", obs.delivery_status),
        ("delivered_date", obs.delivered_date_text),
        ("arriving_by_date", obs.arriving_by_date),
        ("shipping_service", obs.shipping_service),
    ]
    visible = [(k, v) for k, v in pairs if v]
    if visible:
        print("visible:")
        for k, v in visible:
            print(f"  {k:18s} = {v}")
    if obs.tracking_numbers:
        print(f"tracking_numbers ({len(obs.tracking_numbers)}):")
        for t in obs.tracking_numbers:
            print(f"  - {t}")
    if obs.items:
        print(f"items ({len(obs.items)}):")
        for it in obs.items:
            tag = it.item_number or "?"
            qty = it.item_quantity_text or "?"
            tot = it.item_line_total_text or "?"
            title = it.item_title or "?"
            print(f"  - [{tag}] {title}  qty={qty}  total={tot}")
    if obs.refunds:
        print(f"refunds ({len(obs.refunds)}):")
        for r in obs.refunds:
            amt = r.refund_amount_text or "?"
            date = r.refund_date_text or ""
            note = r.refund_note or ""
            print(f"  - {amt}  {date}  {note}".rstrip())
    if payload.unreadable:
        print(f"unreadable ({len(payload.unreadable)}):")
        for u in payload.unreadable:
            print(f"  - {u}")


async def _amain(path: pathlib.Path) -> int:
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    client = OcrClient()
    try:
        try:
            payload, ms = await client.recognize(data)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {path}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    finally:
        await client.aclose()
    _print_human(path, sha, len(data), payload, ms)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Run Stage A on a single screenshot and print what the model saw.")
    p.add_argument("path", type=pathlib.Path)
    args = p.parse_args()
    sys.exit(asyncio.run(_amain(args.path)))


if __name__ == "__main__":
    main()
