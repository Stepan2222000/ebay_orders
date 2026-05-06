from decimal import Decimal

import pytest

from app.stage_b.money import MoneyParseError, parse_money_text


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$49.00", Decimal("49.00")),
        ("US $9.95", Decimal("9.95")),
        ("$49.00 (1 item)", Decimal("49.00")),
        ("$1,234.56", Decimal("1234.56")),
        ("$0.00", Decimal("0.00")),
        ("$30", Decimal("30.00")),
    ],
)
def test_parses_usd(raw: str, expected: Decimal) -> None:
    assert parse_money_text(raw) == expected


def test_none_and_empty() -> None:
    assert parse_money_text(None) is None
    assert parse_money_text("") is None
    assert parse_money_text("   ") is None


def test_free_only_with_flag() -> None:
    assert parse_money_text("Free", allow_free=True) == Decimal("0.00")
    assert parse_money_text("free", allow_free=True) == Decimal("0.00")
    with pytest.raises(MoneyParseError):
        parse_money_text("Free")


@pytest.mark.parametrize(
    "raw",
    ["€49.00", "£10.00", "EUR 49", "C$5.00", "CAD 12", "A$5.00"],
)
def test_rejects_foreign(raw: str) -> None:
    with pytest.raises(MoneyParseError, match="foreign currency"):
        parse_money_text(raw)


@pytest.mark.parametrize(
    "raw",
    ["49", "fortynine", "$$$", "$ ", "12.345"],
)
def test_rejects_garbage(raw: str) -> None:
    with pytest.raises(MoneyParseError):
        parse_money_text(raw)
