"""Strict Decimal helpers for monetary fields."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


class MoneyParseError(ValueError):
    """Raised when a required monetary value is malformed or negative."""


def parse_decimal(value: Any, *, allow_blank: bool = False, field: str = "amount") -> Decimal | None:
    if value is None or str(value).strip() == "":
        if allow_blank:
            return None
        raise MoneyParseError(f"{field} is blank")
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise MoneyParseError(f"{field} is not a Decimal: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise MoneyParseError(f"{field} must be finite and non-negative: {value!r}")
    return parsed


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")

