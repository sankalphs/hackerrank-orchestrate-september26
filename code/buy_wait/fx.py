"""Exact, directed, date-keyed exchange-rate conversion."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

from .models import ExchangeRate


class MissingExchangeRateError(ValueError):
    """A required exact FX row was not supplied by the challenge dataset."""


class DuplicateExchangeRateError(ValueError):
    """Two different rates were supplied for one exact key."""


class RateBook:
    def __init__(self, rates: Iterable[ExchangeRate] = ()):
        self._rates: dict[tuple[date, str, str], Decimal] = {}
        for row in rates:
            key = (row.rate_date, row.from_currency, row.to_currency)
            previous = self._rates.get(key)
            if previous is not None and previous != row.rate:
                raise DuplicateExchangeRateError(f"conflicting rate for {key}")
            self._rates[key] = row.rate

    def get(self, rate_date: date, from_currency: str, to_currency: str) -> Decimal:
        key = (rate_date, from_currency, to_currency)
        try:
            return self._rates[key]
        except KeyError as exc:
            raise MissingExchangeRateError(
                f"no exact FX rate for {rate_date.isoformat()} {from_currency}->{to_currency}"
            ) from exc

    def convert_to_home_currency(
        self,
        amount: Decimal,
        from_currency: str,
        home_currency: str,
        settlement_date: date | None,
    ) -> Decimal:
        if from_currency == home_currency:
            return amount
        if settlement_date is None:
            raise MissingExchangeRateError(
                f"foreign amount has no settlement date: {from_currency}->{home_currency}"
            )
        return amount * self.get(settlement_date, from_currency, home_currency)


def convert_to_home_currency(
    amount: Decimal,
    from_currency: str,
    home_currency: str,
    settlement_date: date | None,
    rates: RateBook,
) -> Decimal:
    """Functional wrapper used by tests and later planner code."""
    return rates.convert_to_home_currency(amount, from_currency, home_currency, settlement_date)

