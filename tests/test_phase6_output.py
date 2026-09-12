from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.candidates import plan_request
from buy_wait.explanations import build_decision_trace, output_row
from buy_wait.fx import RateBook
from buy_wait.models import CashEffect, CanonicalLedger, Event, Lifecycle, Profile
from buy_wait.validator import OutputValidator, ValidationError, write_validated_output


def _ledger(*, balance: str = "1000", minimum: str = "100", credit_after: int | None = None) -> CanonicalLedger:
    profile = Profile("user_test", "USD", Decimal(balance), Decimal(minimum), payment_methods_user_will_consider=("full_payment",))
    events = {}
    effects = []
    lifecycles = {}
    if credit_after is not None:
        event_id = "event_future_credit"
        when = date(2025, 1, 1) + timedelta(days=credit_after)
        event = Event(event_id, "user_test", "income", "Future income", "salary", "credit", Decimal("100"), "USD", when, when, "settled", None, "fixed", None)
        events[event_id] = event
        lifecycles[f"lifecycle:{event_id}"] = Lifecycle(f"lifecycle:{event_id}", "user_test", (event_id,))
        effects.append(CashEffect(event_id, f"lifecycle:{event_id}", "user_test", "credit", Decimal("100"), "USD", when, "settled", "salary", "income", "fixed", None, True, False, "test", (event_id,)))
    return CanonicalLedger(
        profiles={"user_test": profile}, events=events, lifecycles=lifecycles,
        resolved_events={}, effects=effects, rates=RateBook([]),
    )


def _request(*, amount: str = "100", deadline: str = "2025-01-10") -> dict[str, str]:
    return {
        "request_id": "request_test", "user_id": "user_test", "request_date": "2025-01-01",
        "request_type": "purchase", "requested_amount": amount,
        "desired_completion_date": deadline, "allows_partial_payment": "false", "request_text": "test",
    }


class Phase6OutputTests(unittest.TestCase):
    def test_trace_renders_exact_submission_row_and_validates(self) -> None:
        ledger = _ledger()
        request = _request()
        plan = plan_request(ledger, request, horizon_days=90)
        trace = build_decision_trace(plan, ledger)
        row = output_row(plan, trace, ledger)
        validator = OutputValidator(ledger, (request,), {}, horizon_days=90)
        report = validator.validate_rows((row,))
        self.assertEqual(report.row_count, 1)
        self.assertEqual(row["recommended_payment_method"], "full_payment")
        self.assertIn("Pay USD 100 today", row["decision_explanation"])

    def test_validator_resimulates_and_rejects_unsafe_full_payment(self) -> None:
        ledger = _ledger(balance="150", minimum="100")
        request = _request(amount="60")
        row = {
            "request_id": "request_test", "amount_safe_to_pay": "50.00",
            "affordability_status": "affordable_now", "recommended_payment_method": "full_payment",
            "payment_plan": "2025-01-01:60", "earliest_date_for_full_payment": "",
            "spending_changes_needed": "none", "decision_explanation": "unsafe",
        }
        validator = OutputValidator(ledger, (request,), {}, horizon_days=90)
        with self.assertRaises(ValidationError):
            validator.validate_rows((row,))

    def test_phase5_keeps_baseline_earliest_after_deadline(self) -> None:
        ledger = _ledger(balance="100", minimum="50", credit_after=2)
        request = _request(amount="80", deadline="2025-01-02")
        plan = plan_request(ledger, request, horizon_days=90)
        self.assertIsNone(plan.selected.payments[0] if plan.selected.payments else None)
        # The credit arrives on Jan 3 but same-day proposed payments are
        # evaluated before credits, so Jan 4 is the first safe date.
        self.assertEqual(plan.earliest_date_for_full_payment, date(2025, 1, 3))
        self.assertEqual(plan.candidate_horizon_end, date(2025, 3, 31))

    def test_atomic_writer_keeps_last_good_output_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.csv"
            path.write_text("last good\n", encoding="utf-8")
            row = {field: "" for field in ("request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method", "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation")}
            with self.assertRaises(ValidationError):
                write_validated_output((row,), path, lambda _: (_ for _ in ()).throw(ValidationError("bad")))
            self.assertEqual(path.read_text(encoding="utf-8"), "last good\n")


if __name__ == "__main__":
    unittest.main()
