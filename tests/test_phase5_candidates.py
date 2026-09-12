from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.candidates import PaymentCandidate, PaymentOption, plan_request
from buy_wait.forecast import ForecastContext, Payment, SpendingChange, simulate
from buy_wait.models import CashEffect, CanonicalLedger, Event, Lifecycle, Profile
from buy_wait.recurrence import RecurringSeries
from buy_wait.ranking import candidate_sort_key
from buy_wait.spending_changes import eligible_change_actions, search_safe_changes


def _ledger(*, methods: tuple[str, ...] = ("full_payment",), stop: bool = False, balance: str = "200", max_months: int = 3) -> CanonicalLedger:
    profile = Profile(
        "user_test", "USD", Decimal(balance), Decimal("100"),
        expense_categories_user_is_willing_to_stop=("streaming",) if stop else (),
        payment_methods_user_will_consider=methods,
        max_installment_months=max_months,
    )
    events = {}
    effects = []
    for index in range(3):
        event_id = f"event_stream_{index}"
        when = date(2025, 1, 1) + timedelta(days=7 * index)
        event = Event(event_id, "user_test", "subscription", "Stream", "streaming", "debit", Decimal("40"), "USD", when, when, "settled", None, "stoppable", None)
        events[event_id] = event
        effects.append(CashEffect(event_id, f"lifecycle:{event_id}", "user_test", "debit", Decimal("40"), "USD", when, "settled", "streaming", "subscription", "stoppable", None, True, False, "test", (event_id,)))
    return CanonicalLedger(
        profiles={"user_test": profile}, events=events,
        lifecycles={f"lifecycle:{event_id}": Lifecycle(f"lifecycle:{event_id}", "user_test", (event_id,)) for event_id in events},
        resolved_events={}, effects=effects,
    )


class Phase5CandidateTests(unittest.TestCase):
    def test_spending_search_finds_legal_stop(self) -> None:
        ledger = _ledger(stop=True)
        series = (RecurringSeries(
            "series:stream", "event_stream_2", "streaming", "debit", "weekly", Decimal("40"), "USD",
            "stoppable", False, None, (date(2025, 1, 1),), user_id="user_test", source_event_ids=tuple(ledger.events),
        ),)
        context = ForecastContext(ledger, "user_test", date(2025, 1, 2), series)
        actions = eligible_change_actions(context, horizon_end=date(2025, 1, 31))
        self.assertEqual([action.event_id for action in actions], ["event_stream_2"])
        changes = search_safe_changes(context, payments=(Payment(date(2025, 1, 2), Decimal("90")),), horizon_end=date(2025, 1, 31))
        self.assertEqual(changes[0], (SpendingChange("stop", "event_stream_2"),))
        self.assertTrue(simulate(context, extra_payments=(Payment(date(2025, 1, 2), Decimal("90")),), spending_changes=changes[0], horizon_end=date(2025, 1, 31)).safe)

    def test_installment_option_preserves_supplied_schedule(self) -> None:
        ledger = _ledger(methods=("installments",), balance="1000")
        option = PaymentOption("option_test", "request_test", "installments", Decimal("40"), 2, date(2025, 1, 3), 7, Decimal("5"), Decimal("85"))
        plan = plan_request(
            ledger,
            {"request_id": "request_test", "user_id": "user_test", "request_date": "2025-01-02", "desired_completion_date": "2025-01-15", "requested_amount": "80", "allows_partial_payment": "false"},
            options=(option,),
        )
        self.assertEqual(plan.selected.method, "installments")
        self.assertEqual(plan.selected.payment_plan, "2025-01-03:40|2025-01-10:40")
        self.assertEqual(plan.selected.total_paid, Decimal("85"))

    def test_ranking_prefers_no_change_before_cheaper_changed_plan(self) -> None:
        first = PaymentCandidate("full_payment", "affordable_now", (Payment(date(2025, 1, 1), Decimal("10")),), Decimal("10"), Decimal("10"))
        second = PaymentCandidate("installments", "affordable_with_plan", (Payment(date(2025, 1, 1), Decimal("9")),), Decimal("10"), Decimal("9"), (SpendingChange("stop", "event_x"),))
        self.assertLess(candidate_sort_key(first), candidate_sort_key(second))

    def test_installment_limit_is_elapsed_calendar_months_not_payment_count(self) -> None:
        ledger = _ledger(methods=("installments",), balance="2000", max_months=5)
        option = PaymentOption("option_six", "request_test", "installments", Decimal("10"), 6, date(2025, 1, 3), 30, Decimal("5"), Decimal("65"))
        plan = plan_request(
            ledger,
            {"request_id": "request_test", "user_id": "user_test", "request_date": "2025-01-02", "desired_completion_date": "2025-06-15", "requested_amount": "60", "allows_partial_payment": "false"},
            options=(option,),
        )
        self.assertEqual(plan.selected.payment_option_id, "option_six")


if __name__ == "__main__":
    unittest.main()
