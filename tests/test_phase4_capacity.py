from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.capacity import capacity_for_context
from buy_wait.forecast import ForecastContext
from buy_wait.models import CashEffect, CanonicalLedger, Event, Lifecycle, Profile


def _ledger_with_effect(
    *,
    balance: str = "100",
    minimum: str = "50",
    effect_id: str = "event_credit",
    direction: str = "credit",
    amount: str = "100",
    when: date = date(2025, 1, 1),
    methods: tuple[str, ...] = (),
) -> CanonicalLedger:
    profile = Profile(
        "user_test", "USD", Decimal(balance), Decimal(minimum),
        payment_methods_user_will_consider=methods,
    )
    event = Event(
        effect_id, "user_test", "income", "Income", "salary", direction,
        Decimal(amount), "USD", when, when, "settled", None, "fixed", None,
    )
    effect = CashEffect(
        event_id=effect_id,
        lifecycle_id=f"lifecycle:{effect_id}",
        user_id="user_test",
        direction=direction,
        amount=Decimal(amount),
        home_currency="USD",
        effective_date=when,
        status="settled",
        category="salary",
        event_type="income",
        flexibility="fixed",
        minimum_allowed_amount=None,
        included=True,
        reserve=False,
        reason="settled_credit_included",
        source_row_ids=(effect_id,),
    )
    return CanonicalLedger(
        profiles={"user_test": profile},
        events={effect_id: event},
        lifecycles={f"lifecycle:{effect_id}": Lifecycle(f"lifecycle:{effect_id}", "user_test", (effect_id,))},
        resolved_events={},
        effects=[effect],
    )


class Phase4CapacityTests(unittest.TestCase):
    def test_capacity_is_capped_and_rounded_down(self) -> None:
        ledger = _ledger_with_effect(minimum="0")
        context = ForecastContext(ledger, "user_test", date(2025, 1, 1), ())

        result = capacity_for_context(
            context,
            requested_amount=Decimal("80.999"),
        )

        self.assertEqual(result.raw_capacity, Decimal("200"))
        self.assertEqual(result.amount_safe_to_pay, Decimal("80.99"))

    def test_same_day_ordering_shrinks_arithmetic_capacity(self) -> None:
        ledger = _ledger_with_effect()
        context = ForecastContext(ledger, "user_test", date(2025, 1, 1), ())

        result = capacity_for_context(
            context,
            requested_amount=Decimal("150"),
        )

        # Confirmed credits are available before a same-day proposed payment.
        self.assertEqual(result.raw_capacity, Decimal("150"))
        self.assertEqual(result.amount_safe_to_pay, Decimal("150.00"))

    def test_earliest_full_payment_ignores_payment_preferences(self) -> None:
        ledger = _ledger_with_effect(when=date(2025, 1, 2), methods=("installments",))
        context = ForecastContext(ledger, "user_test", date(2025, 1, 1), ())

        result = capacity_for_context(context, requested_amount=Decimal("100"))

        self.assertEqual(result.earliest_date_for_full_payment, date(2025, 1, 2))


if __name__ == "__main__":
    unittest.main()
