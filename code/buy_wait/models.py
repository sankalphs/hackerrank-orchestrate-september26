"""Typed records shared by the Phase 2 ledger and later forecast stages.

The financial engine deliberately keeps ``Decimal`` and ``date`` values typed
past the CSV boundary.  Serialisation belongs at the CLI/report edge so that
float coercion cannot silently change a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...] = ()
    expense_categories_to_protect: tuple[str, ...] = ()
    expense_categories_user_is_willing_to_reduce: tuple[str, ...] = ()
    expense_categories_user_is_willing_to_stop: tuple[str, ...] = ()
    payment_methods_user_will_consider: tuple[str, ...] = ()
    max_installment_months: int | None = None


@dataclass(frozen=True)
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Decimal | None
    currency: str
    event_date: date
    settlement_date: date | None
    status: str
    linked_event_id: str | None
    flexibility: str
    minimum_allowed_amount: Decimal | None


@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: Decimal


@dataclass(frozen=True)
class EvidenceClaim:
    source_id: str
    claim_type: str
    target_event_id: str | None
    amount: Decimal | None = None
    currency: str | None = None
    effective_date: date | None = None
    status: str | None = None
    confidence: Decimal = Decimal("0")
    related_event_id: str | None = None


@dataclass(frozen=True)
class ResolvedEvent:
    """An event after direct evidence overlays, before lifecycle deduplication."""

    event: Event
    amount: Decimal | None
    currency: str
    effective_date: date | None
    status: str
    confirmed: bool
    source_row_ids: tuple[str, ...]
    evidence_source_ids: tuple[str, ...] = ()
    unresolved_reason: str | None = None
    applied_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class CashEffect:
    """The only record type later simulation code should use for cash flow."""

    event_id: str
    lifecycle_id: str
    user_id: str
    direction: str
    amount: Decimal | None
    home_currency: str
    effective_date: date | None
    status: str
    category: str
    event_type: str
    flexibility: str
    minimum_allowed_amount: Decimal | None
    included: bool
    reserve: bool
    reason: str
    source_row_ids: tuple[str, ...]
    evidence_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lifecycle:
    lifecycle_id: str
    user_id: str
    event_ids: tuple[str, ...]


@dataclass
class CanonicalLedger:
    profiles: dict[str, Profile]
    events: dict[str, Event]
    lifecycles: dict[str, Lifecycle]
    resolved_events: dict[str, ResolvedEvent]
    effects: list[CashEffect]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Kept on the ledger so generated future occurrences can use the same
    # exact directed FX policy as the Phase 2 cash effects.  The default keeps
    # hand-built ledgers in unit tests backwards compatible.
    rates: Any = None

    def effects_for_user(self, user_id: str, *, included_only: bool = True) -> list[CashEffect]:
        rows = [effect for effect in self.effects if effect.user_id == user_id]
        if included_only:
            rows = [effect for effect in rows if effect.included]
        return sorted(rows, key=lambda effect: (effect.effective_date or date.max, effect.event_id))
