from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))

from buy_wait.event_resolution import _deduplicate_effects, build_ledger, cash_flow_effect
from buy_wait.fx import MissingExchangeRateError, RateBook
from buy_wait.loaders import load_dataset
from buy_wait.models import CashEffect, Event, Profile, ResolvedEvent


ROOT = Path(__file__).resolve().parents[1]


class Phase2LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger = build_ledger(load_dataset(ROOT / "dataset"))

    def test_profile_is_opening_balance_anchor(self) -> None:
        profile = self.ledger.profiles["user_01"]
        self.assertEqual(profile.current_available_balance, Decimal("58481.1"))
        self.assertEqual(profile.minimum_balance_to_keep, Decimal("18000"))

    def test_exact_directed_fx_only(self) -> None:
        # The real dataset has an exact USD->IDR row on this date.
        rates = load_dataset(ROOT / "dataset").rates
        self.assertEqual(
            rates.convert_to_home_currency(Decimal("2"), "USD", "IDR", date(2025, 8, 15)),
            Decimal("31666.66"),
        )
        with self.assertRaises(MissingExchangeRateError):
            rates.convert_to_home_currency(Decimal("2"), "IDR", "USD", date(2025, 8, 15))

    def test_lifecycle_replacements_are_not_double_counted(self) -> None:
        by_id = {row.event_id: row for row in self.ledger.effects}
        self.assertTrue(by_id["event_12708"].included)
        self.assertFalse(by_id["event_12709"].included)
        self.assertIn("duplicate_linked_cash_effect:event_12708", by_id["event_12709"].reason)
        self.assertFalse(by_id["event_5168"].included)
        self.assertTrue(by_id["event_5169"].included)

    def test_unrealized_and_blank_amounts_are_blocked(self) -> None:
        by_id = {row.event_id: row for row in self.ledger.effects}
        self.assertFalse(by_id["event_1856"].included)
        self.assertEqual(by_id["event_1856"].reason, "non_cash")
        self.assertFalse(by_id["event_253"].included)
        self.assertEqual(by_id["event_253"].reason, "unresolved_amount")

    def test_validated_image_claim_can_resolve_a_blank_amount(self) -> None:
        report = {"records": [{"image_facts": [{
            "source_id": "image_01",
            "related_event_id": "event_253",
            "status": "extracted",
            "amount": "30000000",
            "currency": "IDR",
            "date": "2019-08-31",
        }]}]}
        ledger = build_ledger(load_dataset(ROOT / "dataset"), report)
        self.assertEqual(ledger.resolved_events["event_253"].amount, Decimal("30000000"))
        effect = next(row for row in ledger.effects if row.event_id == "event_253")
        self.assertTrue(effect.included)

    def test_cash_state_matrix_pending_credit_is_excluded(self) -> None:
        event = Event(
            "event_test", "user_01", "refund", "refund", "refund", "credit",
            Decimal("10"), "ZAR", date(2025, 8, 15), date(2025, 8, 15),
            "pending", None, "fixed", None,
        )
        resolved = ResolvedEvent(
            event, event.amount, event.currency, event.settlement_date, event.status,
            False, (event.event_id,),
        )
        effect = cash_flow_effect(
            resolved, lifecycle_id="lifecycle:event_test", home_currency="ZAR", rates=RateBook([])
        )
        self.assertFalse(effect.included)
        self.assertEqual(effect.reason, "pending_credit_excluded")

    def test_later_linked_cancellation_suppresses_prior_cash_effect(self) -> None:
        old = CashEffect(
            "event_old", "lifecycle:event_old", "user_01", "debit", Decimal("10"), "ZAR",
            date(2025, 8, 15), "settled", "rent", "expense", "fixed", None, True, False,
            "settled_debit_included", ("event_old",),
        )
        cancelled = CashEffect(
            "event_cancel", "lifecycle:event_old", "user_01", "debit", None, "ZAR",
            date(2025, 8, 16), "cancelled", "rent", "expense", "fixed", None, False, False,
            "status_cancelled", ("event_cancel",),
        )
        effects, diagnostics = _deduplicate_effects([old, cancelled], {})
        self.assertFalse(effects[0].included)
        self.assertIn("lifecycle_cancelled:event_cancel", effects[0].reason)
        self.assertEqual(diagnostics[0]["kept_event_id"], "event_cancel")


if __name__ == "__main__":
    unittest.main()
