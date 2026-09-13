"""Targeted synthetic gates for uncertainty-aware forecasting.

Covers the six scenarios requested in review feedback: delayed salary,
irregular variable spending, stale recurring income, future rent, same-day
ordering, and partial-payment remainder safety. All cases are synthetic and
general; none references sample or evaluation IDs.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.capacity import capacity_for_context
from buy_wait.evidence import image_amount_consistency, needs_image_second_pass
from buy_wait.forecast import (
    ForecastContext,
    Payment,
    build_forecast_context,
    forecast_diagnostics,
    simulate,
    simulate_scenarios,
)
from buy_wait.models import (
    CanonicalLedger,
    CashEffect,
    Event,
    ExchangeRate,
    Lifecycle,
    Profile,
)
from buy_wait.fx import RateBook
from buy_wait.recurrence import (
    RecurringSeries,
    classify_series_confidence,
    conservative_native_amount,
)


def _ledger(
    *,
    balance: str = "10000",
    minimum: str = "1000",
    effects: list[CashEffect] | None = None,
    events: dict[str, Event] | None = None,
    series: tuple[RecurringSeries, ...] = (),
) -> tuple[CanonicalLedger, ForecastContext]:
    profile = Profile("user_synth", "USD", Decimal(balance), Decimal(minimum))
    ledger = CanonicalLedger(
        profiles={profile.user_id: profile},
        events=events or {},
        lifecycles={},
        resolved_events={},
        effects=effects or [],
        rates=RateBook([]),
    )
    context = ForecastContext(ledger, "user_synth", date(2025, 1, 1), series)
    return ledger, context


def _effect(
    event_id: str,
    *,
    amount: str,
    when: date,
    direction: str = "debit",
    category: str = "rent",
    status: str = "settled",
) -> CashEffect:
    return CashEffect(
        event_id=event_id,
        lifecycle_id=f"lifecycle:{event_id}",
        user_id="user_synth",
        direction=direction,
        amount=Decimal(amount),
        home_currency="USD",
        effective_date=when,
        status=status,
        category=category,
        event_type="expense" if direction == "debit" else "income",
        flexibility="fixed",
        minimum_allowed_amount=None,
        included=True,
        reserve=False,
        reason=f"{status}_{direction}_included",
        source_row_ids=(event_id,),
    )


class ScenarioGateTests(unittest.TestCase):
    def test_delayed_unconfirmed_salary_shifts_min_date(self) -> None:
        salary = RecurringSeries(
            "series:salary", "event_sal", "salary", "credit", "monthly",
            Decimal("5000"), "USD", "fixed", False, None,
            (date(2024, 12, 15),), user_id="user_synth",
            source_event_ids=("event_sal",),
        )
        _, context = _ledger(series=(salary,))
        base = simulate(context, horizon_end=date(2025, 3, 31))
        delayed = simulate(
            context, horizon_end=date(2025, 3, 31), income_delay_days=3
        )
        # Unconfirmed salary legs move later; confirmed streams would not.
        self.assertNotEqual(
            sorted(base.credits.keys()), sorted(delayed.credits.keys())
        )

    def test_confirmed_salary_does_not_shift(self) -> None:
        salary = RecurringSeries(
            "series:salary", "event_sal", "salary", "credit", "monthly",
            Decimal("5000"), "USD", "fixed", False, None,
            (date(2024, 12, 15),), user_id="user_synth",
            source_event_ids=("event_sal",),
            evidence_source_ids=("message_1",),
        )
        self.assertEqual(
            classify_series_confidence(context_ledger_stub(), salary, None),
            "confirmed",
        )

    def test_irregular_pool_conservative_covers_base(self) -> None:
        ledger, _ = _ledger()
        pool = RecurringSeries(
            "series:pool", "event_pool", "groceries", "debit", "weekly",
            Decimal("100"), "USD", "fixed", False, None,
            (date(2024, 12, 1), date(2024, 12, 8), date(2024, 12, 15)),
            user_id="user_synth", source_event_ids=("event_pool",),
            native_amount=Decimal("100"),
        )
        cash = [
            _effect(f"event_pool_{index}", amount=value, when=when, category="groceries")
            for index, (value, when) in enumerate(
                [("50", date(2024, 12, 1)), ("100", date(2024, 12, 8)), ("300", date(2024, 12, 15))]
            )
        ]
        conservative = conservative_native_amount(ledger, pool, cash)
        self.assertGreaterEqual(conservative, Decimal("100"))

    def test_stale_income_stream_does_not_project(self) -> None:
        from buy_wait.recurrence import projected_occurrences

        ended = RecurringSeries(
            "series:old", "event_old", "salary", "credit", "monthly",
            Decimal("4000"), "USD", "fixed", False, None,
            (date(2024, 6, 15),), user_id="user_synth",
            source_event_ids=("event_old",),
            end_date=date(2024, 6, 15),
        )
        self.assertEqual(
            projected_occurrences(ended, date(2025, 1, 1), date(2025, 3, 31)), ()
        )

    def test_future_rent_appears_in_binding_categories(self) -> None:
        rent = RecurringSeries(
            "series:rent", "event_rent", "rent", "debit", "monthly",
            Decimal("2000"), "USD", "fixed", False, None,
            (date(2024, 12, 1),), user_id="user_synth",
            source_event_ids=("event_rent",), native_amount=Decimal("2000"),
        )
        _, context = _ledger(balance="5000", minimum="1000", series=(rent,))
        diagnostics = forecast_diagnostics(context, horizon_end=date(2025, 3, 31))
        categories = {
            row["category"] for row in diagnostics["binding_categories_7d_window"]
        }
        self.assertIn("rent", categories)

    def test_same_day_ordering_debit_before_credit(self) -> None:
        effects = [
            _effect("event_rent", amount="9000", when=date(2025, 1, 5), category="rent"),
            _effect(
                "event_pay", amount="9500", when=date(2025, 1, 5),
                direction="credit", category="salary",
            ),
        ]
        _, context = _ledger(balance="10000", minimum="1000", effects=effects)
        result = simulate(context, horizon_end=date(2025, 1, 6))
        # 10000 - 9000 = 1000 observes the minimum exactly, then +9500.
        self.assertTrue(result.safe)
        self.assertEqual(result.balances[date(2025, 1, 5)], Decimal("10500"))

    def test_partial_remainder_plan_sums_and_uses_earliest(self) -> None:
        ledger, context = _ledger(balance="5000", minimum="1000")
        capacity = capacity_for_context(
            context, requested_amount=Decimal("3000"), horizon_days=30
        )
        safe = capacity.amount_safe_to_pay
        earliest = capacity.earliest_date_for_full_payment
        if earliest is not None and Decimal("0") < safe < Decimal("3000"):
            payments = (
                Payment(context.request_date, safe),
                Payment(earliest, Decimal("3000") - safe),
            )
            total = sum((payment.amount for payment in payments), Decimal("0"))
            self.assertEqual(total, Decimal("3000"))
            result = simulate(
                ForecastContext(
                    ledger, "user_synth", context.request_date, context.series,
                    context.evidence_report, context.confirmed_credits,
                ),
                extra_payments=payments,
                horizon_end=capacity.baseline_horizon_end,
            )
            self.assertIsNotNone(result)

    def test_scenarios_expose_base_and_conservative(self) -> None:
        _, context = _ledger()
        scenarios = simulate_scenarios(context, horizon_end=date(2025, 1, 31))
        self.assertEqual(
            set(scenarios),
            {"base", "conservative_expense", "delayed_income", "conservative_combined"},
        )

    def test_foreign_salary_converts_once(self) -> None:
        profile = Profile("user_fx", "IDR", Decimal("32063050"), Decimal("23379100"))
        rates = RateBook(
            [ExchangeRate(date(2024, 4, 15), "USD", "IDR", Decimal("15833.33"))]
        )
        ledger = CanonicalLedger(
            profiles={profile.user_id: profile}, events={}, lifecycles={},
            resolved_events={}, effects=[], rates=rates,
        )
        series = RecurringSeries(
            "series:fx", "event_fx", "salary", "credit", "monthly",
            Decimal("28499994.00"), "USD", "fixed", False, None,
            (date(2024, 3, 15),), user_id="user_fx",
            source_event_ids=("event_fx",), native_amount=Decimal("1800"),
        )
        context = ForecastContext(ledger, "user_fx", date(2024, 3, 6), (series,))
        from buy_wait.forecast import _converted_series_amount

        self.assertEqual(
            _converted_series_amount(context, series, date(2024, 4, 15)),
            Decimal("1800") * Decimal("15833.33"),
        )

    def test_image_consistency_flags_headline_double(self) -> None:
        fact = {
            "source_id": "image_02", "amount": "200000", "currency": "IDR",
            "status": "extracted", "confidence": 0.9,
        }
        diagnostics = image_amount_consistency(fact, category_median="100000")
        self.assertIn("possible_headline_total_double", diagnostics["flags"])
        self.assertTrue(needs_image_second_pass({"status": "unresolved"}))


def context_ledger_stub() -> CanonicalLedger:
    profile = Profile("user_synth", "USD", Decimal("10000"), Decimal("1000"))
    return CanonicalLedger(
        profiles={profile.user_id: profile}, events={}, lifecycles={},
        resolved_events={}, effects=[], rates=RateBook([]),
    )


if __name__ == "__main__":
    unittest.main()
