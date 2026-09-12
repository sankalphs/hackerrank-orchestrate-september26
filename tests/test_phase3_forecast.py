from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.forecast import ForecastContext, Payment, SpendingChange, simulate
from buy_wait.loaders import load_dataset
from buy_wait.event_resolution import build_ledger
from buy_wait.models import CashEffect, CanonicalLedger, Event, Lifecycle, Profile
from buy_wait.recurrence import RecurringSeries, projected_occurrences


ROOT = Path(__file__).resolve().parents[1]


def _synthetic_ledger(*, balance: str = "100", minimum: str = "50") -> CanonicalLedger:
    profile = Profile("user_test", "USD", Decimal(balance), Decimal(minimum))
    event = Event(
        "event_recurring", "user_test", "subscription", "Plan", "streaming", "debit",
        Decimal("40"), "USD", date(2025, 1, 1), date(2025, 1, 1), "settled", None,
        "stoppable", None,
    )
    effect = CashEffect(
        event_id=event.event_id, lifecycle_id="lifecycle:event_recurring", user_id=event.user_id,
        direction="debit", amount=Decimal("40"), home_currency="USD", effective_date=date(2025, 1, 1),
        status="settled", category="streaming", event_type="subscription", flexibility="stoppable",
        minimum_allowed_amount=None, included=True, reserve=False, reason="settled_debit_included",
        source_row_ids=(event.event_id,),
    )
    return CanonicalLedger(
        profiles={profile.user_id: profile}, events={event.event_id: event},
        lifecycles={"lifecycle:event_recurring": Lifecycle("lifecycle:event_recurring", "user_test", (event.event_id,))},
        resolved_events={}, effects=[effect],
    )


class Phase3ForecastTests(unittest.TestCase):
    def test_month_end_rule_is_explicit_and_stable(self) -> None:
        series = RecurringSeries(
            "series:test", "event_test", "rent", "debit", "monthly", Decimal("100"), "USD",
            "fixed", False, None, (date(2025, 1, 31),), user_id="user_test",
        )
        self.assertEqual(
            projected_occurrences(series, date(2025, 2, 1), date(2025, 4, 30)),
            (date(2025, 2, 28), date(2025, 3, 31), date(2025, 4, 30)),
        )

    def test_distant_future_observation_does_not_block_intervening_legs(self) -> None:
        series = RecurringSeries(
            "series:test", "event_test", "rent", "debit", "monthly", Decimal("100"), "USD",
            "fixed", False, None, (date(2025, 1, 15), date(2025, 6, 15)), user_id="user_test",
        )
        self.assertEqual(
            projected_occurrences(series, date(2025, 2, 1), date(2025, 3, 31)),
            (date(2025, 2, 15), date(2025, 3, 15)),
        )

    def test_same_day_order_observes_debit_before_credit(self) -> None:
        ledger = _synthetic_ledger(balance="100", minimum="50")
        context = ForecastContext(ledger, "user_test", date(2025, 1, 1), ())
        result = simulate(
            context,
            extra_payments=(Payment(date(2025, 1, 2), Decimal("30")),),
            horizon_end=date(2025, 1, 2),
        )
        # No credit exists here; the proposed payment follows required debit.
        self.assertFalse(result.safe)
        self.assertEqual(result.first_violation, date(2025, 1, 2))

    def test_stop_change_suppresses_future_series_legs(self) -> None:
        ledger = _synthetic_ledger(balance="100", minimum="50")
        series = RecurringSeries(
            "series:test", "event_recurring", "streaming", "debit", "weekly", Decimal("40"), "USD",
            "stoppable", False, None, (date(2025, 1, 1),), user_id="user_test",
            source_event_ids=("event_recurring",),
        )
        context = ForecastContext(ledger, "user_test", date(2025, 1, 2), (series,))
        result = simulate(context, spending_changes=(SpendingChange("stop", "event_recurring"),), horizon_end=date(2025, 1, 15))
        self.assertTrue(result.safe)
        self.assertNotIn(date(2025, 1, 8), result.required_debits)

    def test_confirmed_scheduled_salary_is_available_to_forecast(self) -> None:
        dataset = load_dataset(ROOT / "dataset")
        ledger = build_ledger(dataset)
        effect = next(row for row in ledger.effects if row.event_id == "event_103")
        self.assertTrue(effect.included)
        self.assertEqual(effect.amount, Decimal("23320"))

    def test_unscoped_confirmed_evidence_becomes_one_off_credit(self) -> None:
        dataset = load_dataset(ROOT / "dataset")
        evidence = json.loads((ROOT / "code/evaluation/evidence_report.json").read_text(encoding="utf-8"))
        ledger = build_ledger(dataset, evidence)
        from buy_wait.forecast import build_forecast_context

        context = build_forecast_context(ledger, user_id="user_26", request_date=date(2025, 8, 3), evidence_report=evidence)
        result = simulate(context, horizon_end=date(2025, 8, 16))
        self.assertEqual(result.credits[date(2025, 8, 15)], Decimal("30780000"))


if __name__ == "__main__":
    unittest.main()
