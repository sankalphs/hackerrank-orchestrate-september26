"""Deterministic recurring-series inference for the Phase 3 forecast.

The raw event stream contains many ordinary purchases, so recurrence is only
created when dates show a stable cadence.  This module never turns a category
into a daily allowance: it uses observed dates, explicit future events, and
validated evidence as the only sources for projected occurrences.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
import re
from typing import Any, Iterable

from .models import CanonicalLedger, CashEffect


@dataclass(frozen=True)
class RecurringSeries:
    """One recurring cash stream, with its observed occurrence dates."""

    series_id: str
    output_event_id: str
    category: str
    direction: str
    frequency: str
    amount: Decimal
    currency: str
    flexible: str
    protected: bool
    minimum_allowed_amount: Decimal | None
    occurrences: tuple[date, ...]
    user_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    evidence_source_ids: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None


_MONTHLY_MIN = 27
_MONTHLY_MAX = 35
_MAX_CADENCE_DAYS = 35
_NON_RECURRING_CATEGORIES = frozenset({"investment", "windfall", "work_expense"})


def _description_family(effect: CashEffect, description: str) -> str:
    """Keep unrelated salary streams apart while grouping changing labels."""
    if effect.direction != "credit" or effect.category != "salary":
        return "category"
    text = description.casefold()
    if any(word in text for word in ("commission", "bonus", "performance", "arrears", "adjustment", "reimbursement", "prorated")):
        return "variable_income"
    if any(word in text for word in ("salary", "payroll", "employer", "pay")):
        return "regular_salary"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "income"


def _group_key(ledger: CanonicalLedger, effect: CashEffect) -> tuple[str, ...]:
    event = ledger.events.get(effect.event_id)
    family = _description_family(effect, event.description if event else "")
    # Variable income often contains two independent monthly streams (for
    # example payments on the 8th and 22nd) with changing project labels.
    # Their day-of-month anchor is a deterministic, observable discriminator.
    if family == "variable_income" and effect.effective_date is not None:
        family = f"{family}_day_{effect.effective_date.day}"
    return (
        effect.user_id,
        effect.event_type,
        effect.category,
        effect.direction,
        effect.home_currency,
        family,
    )


def _infer_frequency(dates: list[date], *, allow_two: bool = False) -> str | None:
    unique = sorted(set(dates))
    if len(unique) < (2 if allow_two else 3):
        return None
    gaps = [(right - left).days for left, right in zip(unique, unique[1:])]
    if not gaps or any(gap <= 1 or gap > _MAX_CADENCE_DAYS for gap in gaps):
        return None

    # Calendar-month schedules naturally have 28/29/30/31-day gaps.  A stable
    # day-of-month or month-end anchor is safer than forcing a fixed 30 days.
    if len(unique) >= 3 and all(_MONTHLY_MIN <= gap <= _MONTHLY_MAX for gap in gaps):
        return "monthly"

    counts = Counter(gaps)
    cadence, count = counts.most_common(1)[0]
    # Synthetic histories are exact; the 80% rule tolerates one amended or
    # delayed row without turning noisy discretionary spending into a series.
    if count < max(2, (len(gaps) * 4 + 4) // 5):
        return None
    if cadence == 7:
        return "weekly"
    if cadence == 14:
        return "biweekly"
    if cadence == 21:
        return "every_21_days"
    if cadence > 1:
        return f"every_{cadence}_days"
    return None


def _latest_effect(effects: list[CashEffect]) -> CashEffect:
    return max(effects, key=lambda row: (row.effective_date or date.min, row.event_id))


def _series_from_group(
    ledger: CanonicalLedger,
    effects: list[CashEffect],
    *,
    protected_categories: tuple[str, ...],
    allow_two: bool = False,
) -> RecurringSeries | None:
    if not effects:
        return None
    effects = [row for row in effects if row.amount is not None and row.effective_date is not None]
    if not effects:
        return None
    if effects[0].category in _NON_RECURRING_CATEGORIES:
        return None
    dates = sorted({row.effective_date for row in effects if row.effective_date is not None})
    future_hint = any(
        (ledger.events.get(row.event_id) and ledger.events[row.event_id].status == "scheduled")
        for row in effects
    )
    frequency = _infer_frequency(dates, allow_two=allow_two or future_hint)
    if frequency is None:
        return None
    latest = _latest_effect(effects)
    event = ledger.events.get(latest.event_id)
    flexibility = latest.flexibility
    minimum = latest.minimum_allowed_amount
    source_ids = tuple(sorted({row.event_id for row in effects}))
    series_id = "series:" + ":".join((latest.user_id, latest.category, latest.direction, latest.event_type, latest.event_id))
    return RecurringSeries(
        series_id=series_id,
        output_event_id=latest.event_id,
        category=latest.category,
        direction=latest.direction,
        frequency=frequency,
        amount=latest.amount or Decimal("0"),
        currency=(event.currency if event else latest.home_currency),
        flexible=flexibility,
        protected=latest.category in protected_categories,
        minimum_allowed_amount=minimum,
        occurrences=tuple(dates),
        user_id=latest.user_id,
        source_event_ids=source_ids,
        start_date=dates[0] if dates else None,
        end_date=None,
    )


def _facts_for_user(report: dict[str, Any] | None, user_id: str) -> list[dict[str, Any]]:
    if not report:
        return []
    facts: list[dict[str, Any]] = []
    for record in report.get("records", []):
        if isinstance(record, dict) and record.get("user_id") == user_id:
            facts.extend(fact for fact in record.get("message_facts", []) if isinstance(fact, dict))
    return facts


def _match_fact_series(
    ledger: CanonicalLedger,
    series: list[RecurringSeries],
    fact: dict[str, Any],
) -> RecurringSeries | None:
    target = fact.get("target_event_id")
    if target:
        targeted = [row for row in series if target in row.source_event_ids or target == row.output_event_id]
        if targeted:
            return sorted(targeted, key=lambda row: row.series_id)[0]
    amount = fact.get("amount")
    currency = str(fact.get("currency") or "").upper()
    candidates = [row for row in series if row.direction == "credit" and row.category == "salary"]
    if amount is not None:
        try:
            numeric = Decimal(str(amount))
        except Exception:
            numeric = None
        if numeric is not None:
            same_amount = [row for row in candidates if row.amount == numeric]
            if same_amount:
                candidates = same_amount
    if currency:
        same_currency = [row for row in candidates if row.currency == currency]
        if same_currency:
            candidates = same_currency
    return max(candidates, key=lambda row: (row.category == "salary", row.output_event_id), default=None)


def _apply_evidence(
    ledger: CanonicalLedger,
    series: list[RecurringSeries],
    *,
    user_id: str,
    as_of: date,
    report: dict[str, Any] | None,
) -> list[RecurringSeries]:
    facts = _facts_for_user(report, user_id)
    if not facts:
        return series
    result = list(series)
    cancelled_without_target = any(fact.get("claim_type") == "cancel" and not fact.get("target_event_id") for fact in facts)
    for fact in facts:
        claim_type = fact.get("claim_type")
        matched = _match_fact_series(ledger, result, fact)
        if matched is None:
            continue
        index = result.index(matched)
        changed = matched
        source_id = str(fact.get("source_id", ""))
        evidence_ids = tuple(sorted(set((*matched.evidence_source_ids, source_id)))) if source_id else matched.evidence_source_ids
        # Confirmation/date facts without a target are handled as explicit
        # one-off credits by forecast.py.  They must not silently amend a
        # similarly shaped recurring salary stream.
        if claim_type in {"recurrence", "amend", "amount"}:
            amount = matched.amount
            currency = matched.currency
            if fact.get("amount") is not None:
                try:
                    amount = Decimal(str(fact["amount"]))
                except Exception:
                    pass
            if fact.get("currency"):
                currency = str(fact["currency"]).upper()
            occurrences = matched.occurrences
            effective_date = fact.get("effective_date")
            if isinstance(effective_date, str):
                try:
                    effective_date = date.fromisoformat(effective_date)
                except ValueError:
                    effective_date = None
            if effective_date is not None and effective_date >= as_of:
                occurrences = tuple(sorted(set((*occurrences, effective_date))))
            frequency = matched.frequency
            if claim_type == "recurrence":
                explicit_frequency = fact.get("recurrence")
                if isinstance(explicit_frequency, dict):
                    explicit_frequency = explicit_frequency.get("frequency")
                if isinstance(explicit_frequency, str) and explicit_frequency in {
                    "weekly", "biweekly", "monthly", "every_21_days",
                }:
                    frequency = explicit_frequency
                elif frequency is None:
                    frequency = "monthly"
            changed = replace(
                matched,
                amount=amount,
                currency=currency,
                occurrences=occurrences,
                frequency=frequency,
                evidence_source_ids=evidence_ids,
                start_date=min(occurrences) if occurrences else matched.start_date,
            )
        elif claim_type == "cancel":
            changed = replace(matched, end_date=as_of, evidence_source_ids=evidence_ids)
        result[index] = changed

    if cancelled_without_target:
        result = [
            replace(row, end_date=as_of, evidence_source_ids=tuple(sorted(set((*row.evidence_source_ids, "evidence:cancel")))))
            if row.direction == "credit" and row.category == "salary" and row.end_date is None
            else row
            for row in result
        ]
    return result


def infer_recurring_series(
    ledger: CanonicalLedger,
    *,
    user_id: str,
    as_of: date,
    evidence_report: dict[str, Any] | None = None,
) -> tuple[RecurringSeries, ...]:
    """Infer recurring series for one user as of a request date.

    Only included, deduplicated Phase 2 effects participate.  A two-row series
    is accepted only when it is a scheduled future row (or a salary stream),
    which supports the dataset's explicit "next confirmed salary" records
    without making two ordinary purchases a recurrence.
    """
    profile = ledger.profiles[user_id]
    grouped: defaultdict[tuple[str, ...], list[CashEffect]] = defaultdict(list)
    for effect in ledger.effects_for_user(user_id):
        if effect.amount is not None and effect.effective_date is not None:
            grouped[_group_key(ledger, effect)].append(effect)
    series: list[RecurringSeries] = []
    for effects in grouped.values():
        allow_two = bool(effects and effects[0].category == "salary" and effects[0].direction == "credit")
        row = _series_from_group(
            ledger,
            effects,
            protected_categories=profile.expense_categories_to_protect,
            allow_two=allow_two,
        )
        if row is not None:
            series.append(row)
    series = _apply_evidence(ledger, series, user_id=user_id, as_of=as_of, report=evidence_report)
    return tuple(sorted(series, key=lambda row: row.series_id))


def build_recurring_series(
    ledger: CanonicalLedger,
    as_of: date,
    user_id: str | None = None,
    evidence_report: dict[str, Any] | None = None,
) -> dict[str, tuple[RecurringSeries, ...]] | tuple[RecurringSeries, ...]:
    """Build series for one user or all users; convenient Phase 3 entry point."""
    if user_id is not None:
        return infer_recurring_series(ledger, user_id=user_id, as_of=as_of, evidence_report=evidence_report)
    return {
        current_user: infer_recurring_series(ledger, user_id=current_user, as_of=as_of, evidence_report=evidence_report)
        for current_user in sorted(ledger.profiles)
    }


def _month_last_day(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date.resolution).day


def _add_months(value: date, months: int) -> date:
    absolute = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(absolute, 12)
    month = month_index + 1
    last_day = _month_last_day(year, month)
    source_last_day = value.day == _month_last_day(value.year, value.month)
    day = last_day if source_last_day else min(value.day, last_day)
    return date(year, month, day)


def next_occurrence(value: date, frequency: str) -> date:
    if frequency == "monthly":
        return _add_months(value, 1)
    if frequency == "weekly":
        return value + timedelta(days=7)
    if frequency == "biweekly":
        return value + timedelta(days=14)
    if frequency == "every_21_days":
        return value + timedelta(days=21)
    match = re.fullmatch(r"every_(\d+)_days", frequency)
    if match:
        return value + timedelta(days=int(match.group(1)))
    raise ValueError(f"unsupported recurrence frequency: {frequency}")


def projected_occurrences(series: RecurringSeries, start_date: date, end_date: date) -> tuple[date, ...]:
    """Return observed and conservatively generated dates in an inclusive window."""
    if end_date < start_date or series.end_date is not None and start_date > series.end_date:
        return ()
    effective_end = min(end_date, series.end_date) if series.end_date else end_date
    observed = {row for row in series.occurrences if start_date <= row <= effective_end}
    if not series.occurrences or series.frequency == "none":
        return tuple(sorted(observed))
    # A supplied future occurrence may lie beyond this forecast window.  Use
    # the latest known anchor at or before the window end so it cannot prevent
    # generation of intervening recurring legs.
    anchors = [row for row in series.occurrences if row <= effective_end]
    if not anchors:
        return tuple(sorted(observed))
    cursor = max(anchors)
    while cursor < start_date:
        cursor = next_occurrence(cursor, series.frequency)
    while cursor <= effective_end:
        observed.add(cursor)
        cursor = next_occurrence(cursor, series.frequency)
    return tuple(sorted(observed))


__all__ = [
    "RecurringSeries",
    "build_recurring_series",
    "infer_recurring_series",
    "next_occurrence",
    "projected_occurrences",
]
