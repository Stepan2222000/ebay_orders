"""Parse the raw money/text observations from Stage A into USD numerics.

The model's `*_text` fields are kept as raw strings on purpose (SPEC
[[формат сырого распознавания#деньги]]). Stage B is the layer that turns
them into `money_usd_nonneg` numerics for the cleanroom slab. The parser
must:

- accept "$49.00", "US $49.00", "$49.00 (1 item)" — all USD;
- accept "Free" / "free" → 0.00 (only for shipping per SPEC);
- reject foreign-currency-only strings ("€49.00", "£10.00", "C$5.00",
  "EUR 49") because the cleanroom slab is USD-only
  ([[правила денег USD#ошибка-валюты]]).
"""

from __future__ import annotations

import re
from decimal import Decimal


class MoneyParseError(ValueError):
    """Raised when a *_text value cannot be turned into USD numerics."""


_USD_RE = re.compile(
    r"""
    ^\s*
    (?:US\s*)?              # optional "US " prefix
    \$\s*                   # dollar sign mandatory
    (?P<amount>\d{1,9}(?:,\d{3})*(?:\.\d{1,2})?)  # 49 / 49.00 / 1,234.56
    \s*$
    """,
    re.VERBOSE,
)

# Currency markers that are foreign even though they may include "$".
_FOREIGN_DOLLAR_RE = re.compile(r"(?:^|\s|\d)([A-Z]\$|S\$|HK\$|NZ\$|R\$)", re.IGNORECASE)
_FOREIGN_NON_DOLLAR_RE = re.compile(r"(€|£|¥|EUR|GBP|JPY|CAD|AUD|CNY|RUB)", re.IGNORECASE)


def _strip_trailing_paren(value: str) -> str:
    # "$49.00 (1 item)" → "$49.00"
    return re.sub(r"\s*\([^)]*\)\s*$", "", value)


def parse_money_text(value: str | None, *, allow_free: bool = False) -> Decimal | None:
    """Return Decimal USD or None if value is None/empty.

    Raises MoneyParseError on foreign currency or unparseable shape.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if allow_free and raw.lower() in {"free", "free shipping"}:
        return Decimal("0.00")
    cleaned = _strip_trailing_paren(raw)

    # CAD ("C$"), AUD ("A$"), SGD ("S$"), HKD, NZD, BRL — they include $ but
    # are foreign. A bare or "US "-prefixed "$" is USD.
    if _FOREIGN_DOLLAR_RE.search(" " + cleaned):
        raise MoneyParseError(f"foreign currency without USD equivalent: {raw!r}")
    if _FOREIGN_NON_DOLLAR_RE.search(cleaned) and "$" not in cleaned:
        raise MoneyParseError(f"foreign currency without USD equivalent: {raw!r}")

    m = _USD_RE.match(cleaned.replace(",", ""))
    if not m:
        raise MoneyParseError(f"not a USD amount: {raw!r}")
    return Decimal(m.group("amount")).quantize(Decimal("0.01"))
