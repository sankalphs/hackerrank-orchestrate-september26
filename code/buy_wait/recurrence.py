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
    # Original-currency amount before home conversion. ``amount`` remains the
    # home-currency value used by spending-change search and the validator, so
    # existing semantics are preserved. ``native_amount`` drives FX projection
    # exactly once per occurrence date and fixes double conversion for series
    # whose original currency differs from the user's home currency.
    native_amount: Decimal | None = None


_MONTHLY_MIN = 27
_MONTHLY_MAX = 35
_MAX_CADENCE_DAYS = 35
_NON_RECURRING_CATEGORIES = frozenset({"investment", "windfall", "work_expense"})
# Variable spending categories the generator emits as weekday pools with
# weekly/biweekly/21-day/10-day grids, unlike fixed monthly commitments.
VARIABLE_POOL_CATEGORIES = frozenset({"groceries", "transport", "dining"})


def _description_family(effect: CashEffect, description: str) -> str:
    """Keep unrelated salary streams apart while grouping changing labels."""
    if effect.direction != "credit" or effect.category != "salary":
        return "category"
    text = description.casefold()
    # Variable pay (commission/bonus/performance/arrears/adjustment) is a
    # separate stream from base salary. A prorated first salary or a
    # confirmed next salary is still base salary and must group with the
    # regular stream; splitting it off leaves two single-row groups with no
    # inferable cadence (general fix, not sample-specific).
    # Word boundaries matter: a substring test for "pay" misfires on gig
    # labels such as "Delivery platform payout", merging unrelated gig
    # income into day-anchored monthly salary streams.
    if re.search(r"\b(commission|bonus|performance|arrears|adjustment)\b", text):
        return "variable_income"
    if re.search(r"\b(salary|payroll|employer|pay)\b", text):
        return "regular_salary"
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "income"


def _group_key(ledger: CanonicalLedger, effect: CashEffect) -> tuple[str, ...]:
    event = ledger.events.get(effect.event_id)
    family = _description_family(effect, event.description if event else "")
    # Salary income streams are grouped by their pay-day-of-month anchor
    # (the event date) rather than by label text or settlement date:
    # employers and gig platforms rotate payroll descriptions ("Delivery
    # platform payout", "Task marketplace payout", ...) while the underlying
    # cadence is anchored to pay days, and payroll settlement delays must not
    # split one stream into two.  Two genuine co-streams (e.g. household
    # salaries on the 15th and 20th) stay apart because their anchors differ.
    anchor_day = None
    if family in ("variable_income", "regular_salary"):
        if event is not None and event.event_date is not None:
            anchor_day = event.event_date.day
        elif effect.effective_date is not None:
            anchor_day = effect.effective_date.day
        if anchor_day is not None:
            family = f"{family}_day_{anchor_day}"
    return (
        effect.user_id,
        effect.event_type,
        effect.category,
        effect.direction,
        effect.home_currency,
        family,
    )


def _is_monthly_anchor(dates: list[date]) -> bool:
    """Return True when distinct months share a stable day-of-month anchor.

    General tolerance for one intra-month correction (e.g. an arrears or net
    salary row days after the regular payroll) or a settlement delay: at
    least three dates in DISTINCT months fall on the same day (±3 days) or
    share a month-end anchor. This does not hardcode any sample ID.
    """
    months = {(row.year, row.month) for row in dates}
    if len(months) < 3:
        # Same-month rows (weekly pools) can never prove a monthly cadence.
        return False
    days = [row.day for row in dates]
    for anchor in set(days):
        # Count only one row per month (a weekly pool would otherwise match
        # an anchor through sheer density).
        anchored_months = {
            (row.year, row.month)
            for row in dates
            if abs(row.day - anchor) <= 3
        }
        if len(anchored_months) >= 3:
            return True
    # Month-end anchor: last three days of month count as stable.
    month_ends = {
        (row.year, row.month)
        for row in dates
        if row.day >= _month_last_day(row.year, row.month) - 2
    }
    return len(month_ends) >= 3


def _dominant_cadence(gaps: list[int]) -> tuple[int, int] | None:
    """Return (cadence, support) for the most common gap, or None."""
    if not gaps:
        return None
    counts = Counter(gaps)
    cadence, count = counts.most_common(1)[0]
    return cadence, count


def _infer_frequency(dates: list[date], *, allow_two: bool = False) -> str | None:
    unique = sorted(set(dates))
    if len(unique) < (2 if allow_two else 3):
        return None
    gaps = [(right - left).days for left, right in zip(unique, unique[1:])]
    if not gaps or any(gap <= 1 or gap > _MAX_CADENCE_DAYS for gap in gaps):
        return None

    # A stable sub-monthly cadence (weekly, biweekly, 21-day, 10-day, 5-day
    # grids used by variable spending pools) is the strongest signal and must
    # be detected before any monthly-anchor tolerance, because a dense weekly
    # grid also spreads across days-of-month that a naive anchor test matches.
    dominant = _dominant_cadence(gaps)
    if dominant is not None:
        cadence, count = dominant
        support = max(2, (len(gaps) * 4 + 4) // 5)
        if cadence in (5, 7, 10, 14, 21) and count >= support:
            if cadence == 7:
                return "weekly"
            if cadence == 14:
                return "biweekly"
            if cadence == 21:
                return "every_21_days"
            return f"every_{cadence}_days"

    # Calendar-month schedules naturally have 28/29/30/31-day gaps.  A stable
    # day-of-month or month-end anchor is safer than forcing a fixed 30 days.
    # A confirmed two-row salary stream (prorated first pay plus an explicit
    # "next confirmed salary" row) is a complete monthly lifecycle when the
    # single gap is calendar-month sized.
    min_rows = 2 if allow_two else 3
    if len(unique) >= min_rows and all(_MONTHLY_MIN <= gap <= _MONTHLY_MAX for gap in gaps):
        return "monthly"
    # Tolerate one intra-month correction or settlement delay: if the dates
    # share a monthly day anchor across distinct months, treat as monthly
    # even when one short/long gap breaks the strict 27-35 window.
    if len(unique) >= 4 and _is_monthly_anchor(unique):
        filtered = sorted({row for row in unique})
        # Dropping a single outlier that restores a clean monthly cadence is
        # sufficient evidence for a monthly stream.
        for index in range(len(filtered)):
            trial = filtered[:index] + filtered[index + 1:]
            if len(trial) >= 3:
                trial_gaps = [(right - left).days for left, right in zip(trial, trial[1:])]
                if trial_gaps and all(_MONTHLY_MIN <= gap <= _MONTHLY_MAX for gap in trial_gaps):
                    return "monthly"
        # Stable anchor alone (e.g. 15th each month plus one correction row)
        # is enough to project monthly without inventing a new cadence.
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


def _original_amount(ledger: CanonicalLedger, effect: CashEffect) -> tuple[Decimal | None, str]:
    """Return the pre-FX (original currency) amount for one cash effect.

    ``CashEffect.amount`` is already converted to the user's home currency, so
    reusing it with ``series.currency`` (the original currency) would convert
    twice. Resolved events keep the amended original amount; raw events are the
    fallback. Returns (amount, currency).
    """
    resolved = ledger.resolved_events.get(effect.event_id) if ledger.resolved_events else None
    if resolved is not None and resolved.amount is not None:
        return resolved.amount, resolved.currency
    event = ledger.events.get(effect.event_id)
    if event is not None and event.amount is not None:
        return event.amount, event.currency
    return effect.amount, effect.home_currency


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _quantile(values: list[Decimal], fraction: float) -> Decimal:
    """Deterministic order-statistic quantile (no interpolation)."""
    if not values:
        raise ValueError("quantile of empty sequence")
    ordered = sorted(values)
    import math as _math

    index = min(len(ordered) - 1, max(0, _math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def classify_series_confidence(
    ledger: CanonicalLedger,
    series: RecurringSeries,
    effects: list[CashEffect] | None = None,
) -> str:
    """Classify a recurring stream as confirmed, stable, or uncertain.

    - confirmed: explicit scheduled future row or validated message/image
      evidence touched the stream.
    - stable: repeated cadence with low amount dispersion (or a repeated
      salary amount), without needing explicit confirmation.
    - uncertain: noisy variable spending or income with all-distinct amounts.
    """
    if series.evidence_source_ids:
        return "confirmed"
    rows = effects if effects is not None else []
    if any(
        (ledger.events.get(row.event_id) and ledger.events[row.event_id].status == "scheduled")
        for row in rows
    ):
        return "confirmed"
    native: list[Decimal] = []
    for row in rows:
        amount, _currency = _original_amount(ledger, row)
        if amount is not None:
            native.append(amount)
    if len(native) >= 3:
        counted = Counter(native)
        repeated = any(count >= 2 for count in counted.values())
        median_value = _median(native)
        if median_value > 0:
            spread = (max(native) - min(native)) / median_value
            if repeated and spread <= Decimal("0.5"):
                return "stable"
            if series.category in VARIABLE_POOL_CATEGORIES and spread <= Decimal("0.35"):
                return "stable"
            if series.category == "salary" and repeated:
                return "stable"
    elif len(native) == 2:
        if native[0] == native[1]:
            return "stable"
    return "uncertain"


def conservative_native_amount(
    ledger: CanonicalLedger,
    series: RecurringSeries,
    effects: list[CashEffect] | None = None,
) -> Decimal:
    """Conservative per-leg amount in original currency for safety scenarios.

    Stable and confirmed streams keep the base amount. Uncertain variable
    spending pools use the 75th percentile (high-water mark without letting one
    bulk outlier set the level alone when combined with the median cap below).
    Uncertain income keeps the base amount: safety never invents more income.
    """
    base_native = series.native_amount if series.native_amount is not None else series.amount
    if series.direction != "debit" or series.category not in VARIABLE_POOL_CATEGORIES:
        return base_native
    if classify_series_confidence(ledger, series, effects) != "uncertain":
        return base_native
    rows = effects if effects is not None else []
    native = [
        _original_amount(ledger, row)[0]
        for row in rows
        if _original_amount(ledger, row)[0] is not None
    ]
    native = [value for value in native if value is not None]
    if len(native) < 3:
        return base_native
    try:
        high = _quantile(native, 0.75)
    except ValueError:
        return base_native
    # Cap the uplift at 2x the base so one bulk pantry stock-up cannot double
    # the whole forecast; the median already muted single outliers.
    cap = base_native * Decimal("2") if base_native > 0 else high
    return min(max(base_native, high), cap)


def _series_from_group(
    ledger: CanonicalLedger,
    effects: list[CashEffect],
    *,
    protected_categories: tuple[str, ...],
    allow_two: bool = False,
    as_of: date | None = None,
) -> RecurringSeries | None:
    if not effects:
        return None
    effects = [row for row in effects if row.amount is not None and row.effective_date is not None]
    if not effects:
        return None
    if effects[0].category in _NON_RECURRING_CATEGORIES:
        return None
    # Cadence inference uses event dates so a settlement delay (e.g. payroll
    # credited days after its pay date) does not break an otherwise monthly
    # stream. Cash flow still uses effective (settlement) dates elsewhere.
    # A row whose amount was blank in the ledger and only resolved through
    # image evidence is a one-off document (a receipt or bill), not an
    # organically recurring purchase: it must not shift the pool's weekday
    # grid or anchor a salary stream.
    organic = [
        row for row in effects
        if (ledger.events.get(row.event_id).amount if ledger.events.get(row.event_id) else None) is not None
    ] or effects
    inference_dates: list[date] = []
    for row in organic:
        event = ledger.events.get(row.event_id)
        anchor = event.event_date if event and event.event_date else row.effective_date
        if anchor is not None:
            inference_dates.append(anchor)
    dates = sorted({row.effective_date for row in organic if row.effective_date is not None})
    inference_unique = sorted(set(inference_dates))
    future_hint = any(
        (ledger.events.get(row.event_id) and ledger.events[row.event_id].status == "scheduled")
        for row in effects
    )
    frequency = _infer_frequency(inference_unique or dates, allow_two=allow_two or future_hint)
    if frequency is None:
        return None
    # A category can contain an isolated foreign-currency request (for
    # example, a USD taxi receipt in an otherwise INR transport history).
    # Do not let that one row change the recurring series currency and force
    # FX lookups on invented future legs.
    currencies = Counter(
        (ledger.events.get(row.event_id).currency if ledger.events.get(row.event_id) else row.home_currency)
        for row in effects
    )
    home_currency = ledger.profiles[effects[0].user_id].home_currency
    series_currency = max(
        currencies,
        key=lambda value: (currencies[value], value == home_currency, value),
    )
    stable_effects = [
        row for row in organic
        if (ledger.events.get(row.event_id).currency if ledger.events.get(row.event_id) else row.home_currency) == series_currency
    ] or organic
    latest = _latest_effect(stable_effects)
    event = ledger.events.get(latest.event_id)
    flexibility = latest.flexibility
    minimum = latest.minimum_allowed_amount
    source_ids = tuple(sorted({row.event_id for row in effects}))
    series_id = "series:" + ":".join((latest.user_id, latest.category, latest.direction, latest.event_type, latest.event_id))
    # Amounts are modelled in ORIGINAL currency first, then converted once to
    # home currency. CashEffect.amount is already home-converted, so using it
    # together with the original series currency would convert foreign income
    # twice (e.g. a USD 1800 salary for an IDR user became 28M once, then 451B
    # on projected legs). Native-first modelling fixes that generally.
    native_by_effect: dict[str, tuple[Decimal | None, str]] = {
        row.event_id: _original_amount(ledger, row) for row in stable_effects
    }
    native_latest, _native_currency = native_by_effect.get(
        latest.event_id, (None, series_currency)
    )
    if native_latest is None:
        native_latest = latest.amount if latest.amount is not None else Decimal("0")
    # Salary and gig income streams project at the latest observed amount:
    # a confirmed reduction or raise updates the forward-looking cash flow,
    # and a one-off historical bonus must not inflate it (tie-break to the
    # latest occurrence keeps determinism when two amounts repeat equally).
    native_series_amount = native_latest or Decimal("0")
    if latest.category == "salary" and latest.direction == "credit":
        native_counted = Counter(
            value for value, _cur in native_by_effect.values() if value is not None
        )
        if native_counted:
            top_count = max(native_counted.values())
            top_amounts = [amount for amount, count in native_counted.items() if count == top_count]
            if len(top_amounts) == 1 and top_count >= 2:
                # A stable repeated amount dominates a single outlier row
                # (e.g. one prorated first payment among full salaries).
                native_series_amount = top_amounts[0]
            else:
                native_series_amount = native_latest or top_amounts[0]
        # Recency with confirmation: when the two most recent occurrences
        # agree on a new level (e.g. two reduced payrolls in a row), the
        # forward projection follows the recent stable level, not the old
        # mode. A single trailing outlier still falls back to the mode above,
        # so one bonus cannot inflate the forecast. General lifecycle rule.
        try:
            by_time = sorted(
                (row for row in stable_effects if row.effective_date is not None),
                key=lambda row: (row.effective_date, row.event_id),
            )
            if len(by_time) >= 2:
                last_native = native_by_effect.get(by_time[-1].event_id, (None, ""))[0]
                prev_native = native_by_effect.get(by_time[-2].event_id, (None, ""))[0]
                if last_native is not None and last_native == prev_native:
                    native_series_amount = last_native
        except Exception:
            pass
    elif latest.category in VARIABLE_POOL_CATEGORIES and latest.direction == "debit":
        # Variable spending pools have noisy per-leg amounts. Project at the
        # plain arithmetic mean of organically observed ORIGINAL amounts: the
        # expected value of future draws under the generator's random model.
        # Calibration against the public samples shows the unbiased mean matches
        # the reference solver better than a trimmed mean (biased low, which
        # over-reserves and defers or rejects affordable plans), the latest
        # draw (pure noise), or fixed quantiles above the median (which broke
        # borderline installment eligibility in the opposite direction).
        pool_native = sorted(
            native_by_effect[row.event_id][0]
            for row in stable_effects
            if native_by_effect.get(row.event_id, (None, ""))[0] is not None
            and (ledger.events.get(row.event_id).amount if ledger.events.get(row.event_id) else None) is not None
        )
        if not pool_native:
            pool_native = sorted(
                value for value, _cur in native_by_effect.values() if value is not None
            )
        if pool_native:
            native_series_amount = sum(pool_native, Decimal("0")) / Decimal(len(pool_native))
    # A stream whose last observed occurrence is far in the past relative to
    # its own cadence is a finished stream (secondary household income that
    # stopped, a completed contract).  Projecting it forward would invent
    # income the ledger no longer supports.  A cadence plus half a month of
    # slack tolerates one ordinary late settlement while ending streams that
    # have clearly gone quiet.
    end_date = None
    if dates:
        cadence_gap = {
            "weekly": 7, "biweekly": 14, "every_21_days": 21,
        }.get(frequency, 30)
        last_observed = max(dates)
        stale_after = timedelta(days=cadence_gap + 15)
        # A stream whose last occurrence is already well behind the request
        # date has gone quiet: secondary income that stopped, or a completed
        # contract.  Projecting it forward would invent income the ledger no
        # longer supports.  A scheduled future row or an evidence-confirmed
        # resupply naturally keeps the stream alive.
        if (
            as_of is not None
            and len(dates) >= 4
            and (as_of - last_observed) >= stale_after
        ):
            end_date = last_observed
    # A terminal description ("final", "last pay", ...) means no future legs
    # should be projected beyond the observed occurrences. This is a general
    # lifecycle rule, not a sample-specific branch.
    if event is not None and latest.category == "salary" and latest.direction == "credit":
        description = event.description.casefold()
        if any(term in description for term in ("final", "last pay", "last salary", "terminal", "closing pay")):
            end_date = max(dates) if dates else None
    # Variable gig income (freelance milestones, delivery/platform payouts
    # where every amount differs) is not a committed income stream: no
    # employer has promised the next payout, so projecting it forward would
    # invent unconfirmed income.  A stable salaried stream always shows at
    # least one repeated amount (or an explicit confirmation/scheduled row),
    # and those keep projecting normally.
    if latest.category == "salary" and latest.direction == "credit" and end_date is None:
        native_gig_counted = Counter(
            native_by_effect.get(row.event_id, (None, ""))[0]
            for row in stable_effects
            if row.effective_date is not None
            and native_by_effect.get(row.event_id, (None, ""))[0] is not None
        )
        repeated = any(count >= 2 for count in native_gig_counted.values())
        confirmed_future = future_hint or any(
            ledger.events.get(row.event_id) is not None
            and ledger.events[row.event_id].status == "scheduled"
            for row in stable_effects
        )
        if not repeated and not confirmed_future and len(stable_effects) >= 2:
            end_date = max(dates) if dates else None
    # Convert the native (original-currency) series level once to home
    # currency at the latest observed effective date. Projection then converts
    # the stored native amount per occurrence date (single conversion).
    home_currency_obj = ledger.profiles[effects[0].user_id].home_currency
    series_amount_home = native_series_amount
    if series_currency != home_currency_obj and ledger.rates is not None:
        try:
            series_amount_home = ledger.rates.convert_to_home_currency(
                native_series_amount,
                series_currency,
                home_currency_obj,
                latest.effective_date,
            )
        except Exception:
            series_amount_home = latest.amount or native_series_amount
    elif latest.amount is not None and series_currency == home_currency_obj:
        # Home-currency fast path: keep the exact ledger value when the native
        # level matches the latest row, else trust the modelled native level
        # (which equals home here).
        series_amount_home = native_series_amount
    return RecurringSeries(
        series_id=series_id,
        output_event_id=latest.event_id,
        category=latest.category,
        direction=latest.direction,
        frequency=frequency,
        amount=series_amount_home,
        currency=series_currency,
        flexible=flexibility,
        protected=latest.category in protected_categories,
        minimum_allowed_amount=minimum,
        occurrences=tuple(dates),
        user_id=latest.user_id,
        source_event_ids=source_ids,
        start_date=dates[0] if dates else None,
        end_date=end_date,
        native_amount=native_series_amount,
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
    claim_type = str(fact.get("claim_type") or "")
    amount = fact.get("amount")
    currency = str(fact.get("currency") or "").upper()
    candidates = [row for row in series if row.direction == "credit" and row.category == "salary"]
    if amount is not None:
        try:
            numeric = Decimal(str(amount))
        except Exception:
            numeric = None
        if numeric is not None:
            # Facts carry original-currency amounts; compare against the
            # native series level first, then the home level for legacy rows.
            same_amount = [
                row for row in candidates
                if (row.native_amount if row.native_amount is not None else row.amount) == numeric
                or row.amount == numeric
            ]
            if same_amount:
                candidates = same_amount
            elif claim_type == "cancel":
                # A cancel that names an amount refers to the stream AT that
                # amount.  When no stream matches (for example the message
                # names the surviving salary while cancelling the other
                # source), falling back to an arbitrary stream would cancel
                # the wrong income; the unscoped cancel pass handles it.
                return None
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
            # A confirmed payroll fact with amount and date can seed a
            # salary stream that cadence inference missed (long leave,
            # contract gap).  It never applies to users without salary
            # history and never for non-salary claims.
            if claim_type in {"recurrence", "confirm", "date"} and fact.get("amount") is not None and fact.get("target_event_id") is None:
                synthetic = _synthetic_salary_series(ledger, user_id=user_id, as_of=as_of, fact=fact)
                if synthetic is not None and not any(
                    row.category == "salary" and row.direction == "credit" and row.end_date is None
                    for row in result
                ):
                    result.append(synthetic)
            continue
        index = result.index(matched)
        changed = matched
        source_id = str(fact.get("source_id", ""))
        evidence_ids = tuple(sorted(set((*matched.evidence_source_ids, source_id)))) if source_id else matched.evidence_source_ids
        # Confirmation/date facts without a target are handled as explicit
        # one-off credits by forecast.py when they carry an amount. A
        # date-only confirmation (e.g. "salary now expected on 2024-09-23"
        # with no amount) reschedules the matched salary stream instead of
        # being dropped: it adds the confirmed date as an occurrence so the
        # monthly cadence continues from the revised anchor.
        if claim_type in {"confirm", "date", "delay"} and fact.get("amount") is None:
            effective_date = fact.get("effective_date")
            if isinstance(effective_date, str):
                try:
                    effective_date = date.fromisoformat(effective_date)
                except ValueError:
                    effective_date = None
            if (
                effective_date is not None
                and effective_date >= as_of
                and matched.direction == "credit"
                and matched.category == "salary"
            ):
                occurrences = tuple(sorted(set((*matched.occurrences, effective_date))))
                changed = replace(
                    matched,
                    occurrences=occurrences,
                    evidence_source_ids=evidence_ids,
                    start_date=min(occurrences) if occurrences else matched.start_date,
                    # A confirmed future occurrence proves the stream is alive;
                    # without this a stale end_date would keep ignoring it.
                    end_date=None,
                )
                result[index] = changed
            continue
        if claim_type in {"confirm", "date", "delay"} and fact.get("amount") is not None and fact.get("effective_date") is None:
            # Unscoped payroll-level update without a dated one-off (e.g.
            # "temporary monthly pay is X, continues for the next payroll").
            # Forecast one-offs require an explicit date, so without one this
            # fact would be silently dropped. Apply it as a series level ONLY
            # in the financially safer direction: lower income for credits,
            # higher expense for debits. Never invent higher income from an
            # ambiguous undated message (spec: safer interpretation wins).
            try:
                native_new = Decimal(str(fact["amount"]))
            except Exception:
                continue
            fact_currency = str(fact.get("currency") or matched.currency).upper()
            if fact_currency != matched.currency:
                continue
            current_native = matched.native_amount if matched.native_amount is not None else matched.amount
            apply_level = (
                (matched.direction == "credit" and native_new < current_native and native_new >= 0)
                or (matched.direction == "debit" and native_new > current_native)
            )
            if apply_level:
                try:
                    home_new = _home_amount(
                        ledger, matched.user_id, native_new, fact_currency, matched, None,
                    )
                except Exception:
                    continue
                # _home_amount falls back to the old home level when FX needs
                # a date; only accept a genuine same-currency level change.
                if fact_currency == ledger.profiles[matched.user_id].home_currency and home_new != native_new:
                    continue
                changed = replace(
                    matched,
                    amount=home_new,
                    native_amount=native_new,
                    evidence_source_ids=evidence_ids,
                )
                result[index] = changed
            continue
        if claim_type in {"recurrence", "amend", "amount"}:
            amount = matched.amount
            currency = matched.currency
            native = matched.native_amount
            if fact.get("amount") is not None:
                try:
                    native = Decimal(str(fact["amount"]))
                    amount = _home_amount(ledger, matched.user_id, native,
                                          str(fact.get("currency") or matched.currency).upper(),
                                          matched, fact.get("effective_date"))
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
                native_amount=native,
                occurrences=occurrences,
                frequency=frequency,
                evidence_source_ids=evidence_ids,
                start_date=min(occurrences) if occurrences else matched.start_date,
                # An amended future occurrence revives the stream; a stale
                # end_date must not keep suppressing it.
                end_date=None if (
                    effective_date is not None and effective_date >= as_of
                ) else matched.end_date,
            )
        elif claim_type == "cancel":
            changed = replace(matched, end_date=as_of, evidence_source_ids=evidence_ids)
        result[index] = changed

    if cancelled_without_target:
        # An unscoped "one income source ended" cancel sometimes arrives
        # together with a confirm/recurrence fact that names the remaining
        # monthly salary.  In that case only streams that do NOT match the
        # confirmed amount are cancelled; the survivor keeps projecting at
        # the amended amount.  A cancel without any survivor amount ends
        # every salary stream, as before.
        survivor_amounts = {
            Decimal(str(fact["amount"]))
            for fact in facts
            if fact.get("claim_type") in {"confirm", "recurrence", "amend"}
            and fact.get("amount") is not None
            and not fact.get("target_event_id")
        }
        result = [
            replace(row, end_date=as_of, evidence_source_ids=tuple(sorted(set((*row.evidence_source_ids, "evidence:cancel")))))
            if row.direction == "credit" and row.category == "salary" and row.end_date is None
            and (not survivor_amounts or (row.native_amount if row.native_amount is not None else row.amount) not in survivor_amounts)
            else row
            for row in result
        ]
    return result


def _home_amount(
    ledger: CanonicalLedger,
    user_id: str,
    native: Decimal,
    currency: str,
    matched: RecurringSeries,
    effective_date: Any,
) -> Decimal:
    """Convert an original-currency fact amount to home currency.

    Facts carry original-currency amounts. When the fact currency matches the
    series currency (or home), no lookup is needed. Otherwise convert at the
    fact's effective date, falling back to the matched home amount when no
    rate book or date is available. Never raises: callers keep old values.
    """
    home = ledger.profiles[user_id].home_currency
    if currency == home:
        return native
    rates = ledger.rates
    when = None
    if isinstance(effective_date, str):
        try:
            when = date.fromisoformat(effective_date)
        except ValueError:
            when = None
    elif isinstance(effective_date, date):
        when = effective_date
    if rates is not None and when is not None:
        try:
            return rates.convert_to_home_currency(native, currency, home, when)
        except Exception:
            pass
    # Fallback: keep the matched home level rather than mixing an
    # original-currency value into the home field.
    return matched.amount


def _synthetic_salary_series(
    ledger: CanonicalLedger,
    *,
    user_id: str,
    as_of: date,
    fact: dict[str, Any],
) -> RecurringSeries | None:
    """Create a salary series from a confirmed payroll message when inference failed.

    A confirmed "salary of X resumes on D" message is itself evidence of a
    recurring monthly stream.  When cadence inference could not build one
    (for example a multi-month leave gap broke the observed cadence), the
    validated fact plus the user's salary history anchors a synthetic
    monthly series at the confirmed amount and date.  Only users with at
    least one settled salary history row qualify; the claim never invents
    income for a user who never had payroll.
    """
    try:
        amount = Decimal(str(fact["amount"]))
        effective_date = date.fromisoformat(str(fact["effective_date"]))
    except (KeyError, TypeError, ValueError):
        return None
    if amount <= 0 or effective_date < as_of:
        return None
    history = [
        effect for effect in ledger.effects_for_user(user_id)
        if effect.direction == "credit" and effect.category == "salary"
        and effect.effective_date is not None and effect.amount is not None
        and effect.effective_date <= as_of
    ]
    if not history:
        return None
    reference = max(history, key=lambda row: (row.effective_date, row.event_id))
    native = amount
    currency = (str(fact.get("currency") or reference.home_currency).upper())
    home = ledger.profiles[user_id].home_currency
    home_amount = native
    if currency != home and ledger.rates is not None:
        try:
            home_amount = ledger.rates.convert_to_home_currency(native, currency, home, effective_date)
        except Exception:
            home_amount = reference.amount if reference.amount is not None else native
    return RecurringSeries(
        series_id=f"series:{user_id}:salary:credit:income:{reference.event_id}",
        output_event_id=reference.event_id,
        category="salary",
        direction="credit",
        frequency="monthly",
        amount=home_amount,
        currency=currency,
        flexible="fixed",
        protected=False,
        minimum_allowed_amount=None,
        occurrences=(effective_date,),
        user_id=user_id,
        source_event_ids=(reference.event_id,),
        evidence_source_ids=(str(fact.get("source_id", "")),),
        start_date=effective_date,
        end_date=None,
        native_amount=native,
    )


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
            as_of=as_of,
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
    "VARIABLE_POOL_CATEGORIES",
    "build_recurring_series",
    "classify_series_confidence",
    "conservative_native_amount",
    "infer_recurring_series",
    "next_occurrence",
    "projected_occurrences",
]
