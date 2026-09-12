"""Deterministic search for legal recurring-spending changes.

The forecast simulator owns the meaning of a change.  This module only
enumerates the changes the profile permits and searches combinations of at
most three actions.  It never changes protected or fixed spending and never
uses a greedy action as a substitute for checking the resulting forecast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from itertools import combinations
from typing import Iterable

from .forecast import ForecastContext, Payment, SpendingChange, simulate
from .recurrence import RecurringSeries, projected_occurrences


class SpendingChangeError(ValueError):
    """Raised when a spending-change request is malformed."""


@dataclass(frozen=True)
class ChangeAction:
    """One profile-permitted action on a recurring debit series."""

    event_id: str
    action: str
    current_amount: Decimal
    minimum_amount: Decimal | None
    category: str
    series_id: str

    @property
    def signature(self) -> str:
        if self.action == "stop":
            return f"stop:{self.event_id}"
        return f"reduce_to:{self.event_id}:{self.minimum_amount}"


def _series_event_id(series: RecurringSeries) -> str:
    # The output event is the stable, user-visible identifier used by the
    # dataset examples.  Source IDs remain accepted by the simulator.
    return series.output_event_id or (series.source_event_ids[0] if series.source_event_ids else series.series_id)


def eligible_change_actions(
    context: ForecastContext,
    *,
    horizon_end: date,
) -> tuple[ChangeAction, ...]:
    """Return all legal actions for active, flexible recurring debits."""
    profile = context.ledger.profiles[context.user_id]
    actions: list[ChangeAction] = []
    for series in context.series:
        if series.user_id and series.user_id != context.user_id:
            continue
        if series.direction != "debit" or series.protected:
            continue
        if not projected_occurrences(series, context.request_date, horizon_end):
            continue
        if not series.flexible or series.flexible == "fixed":
            continue
        event_id = _series_event_id(series)
        current = series.amount
        flexible = series.flexible
        if (
            flexible in {"stoppable", "reducible_or_stoppable"}
            and series.category in profile.expense_categories_user_is_willing_to_stop
        ):
            actions.append(ChangeAction(event_id, "stop", current, None, series.category, series.series_id))
        if (
            flexible in {"reducible", "reducible_or_stoppable"}
            and series.category in profile.expense_categories_user_is_willing_to_reduce
        ):
            minimum = series.minimum_allowed_amount if series.minimum_allowed_amount is not None else Decimal("0")
            if minimum < current:
                actions.append(ChangeAction(event_id, "reduce_to", current, minimum, series.category, series.series_id))
    return tuple(sorted(actions, key=lambda row: (row.event_id, row.action, row.series_id)))


def _as_change(action: ChangeAction) -> SpendingChange:
    return SpendingChange(action.action, action.event_id, action.minimum_amount)


def _valid_combination(actions: Iterable[ChangeAction]) -> bool:
    ids = [action.event_id for action in actions]
    return len(ids) == len(set(ids))


def _changes_for(actions: tuple[ChangeAction, ...]) -> tuple[SpendingChange, ...]:
    return tuple(_as_change(action) for action in actions)


def search_safe_changes(
    context: ForecastContext,
    *,
    payments: Iterable[Payment],
    horizon_end: date,
    max_changes: int = 3,
) -> tuple[tuple[SpendingChange, ...], ...]:
    """Find every safe legal change set up to ``max_changes``.

    A reducible action is evaluated at its permitted minimum.  This is the
    conservative target required by the plan: if that target cannot make the
    proposed schedule safe, no larger target can.  Combination enumeration is
    exhaustive for the available action set and deterministic by signature.
    """
    if max_changes < 0:
        raise SpendingChangeError("max_changes must be non-negative")
    payment_rows = tuple(payments)
    if simulate(context, extra_payments=payment_rows, horizon_end=horizon_end).safe:
        return ((),)
    actions = eligible_change_actions(context, horizon_end=horizon_end)
    safe_sets: list[tuple[SpendingChange, ...]] = []
    upper = min(max_changes, len(actions))
    for size in range(1, upper + 1):
        for selected in combinations(actions, size):
            if not _valid_combination(selected):
                continue
            changes = _changes_for(selected)
            result = simulate(
                context,
                extra_payments=payment_rows,
                spending_changes=changes,
                horizon_end=horizon_end,
            )
            if result.safe:
                safe_sets.append(changes)
    return tuple(sorted(safe_sets, key=_change_signature))


def _change_signature(changes: Iterable[SpendingChange]) -> str:
    ordered = sorted(changes, key=lambda change: (change.event_id, change.action, change.new_amount or Decimal("0")))
    return "|".join(_format_change(change) for change in ordered)


def _format_change(change: SpendingChange) -> str:
    if change.action == "stop":
        return f"stop:{change.event_id}"
    return f"reduce_to:{change.event_id}:{change.new_amount}"


def spending_change_text(changes: Iterable[SpendingChange]) -> str:
    """Serialize legal changes in the output contract's order."""
    ordered = sorted(changes, key=lambda change: (change.event_id, change.action, change.new_amount or Decimal("0")))
    rows = [_format_change(change) for change in ordered]
    return "|".join(rows) if rows else "none"


__all__ = [
    "ChangeAction",
    "SpendingChangeError",
    "eligible_change_actions",
    "search_safe_changes",
    "spending_change_text",
]
