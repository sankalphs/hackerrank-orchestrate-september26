"""Single-authority deterministic balance simulation for Phase 3."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .models import CanonicalLedger, CashEffect
from .recurrence import RecurringSeries, infer_recurring_series, projected_occurrences
from .config import DATASET_DIR, EVIDENCE_OUTPUT, FORECAST_OUTPUT
from .event_resolution import build_ledger_from_directory
from .loaders import parse_date, read_csv


class ForecastError(ValueError):
    """Raised when a forecast cannot safely resolve its inputs."""


@dataclass(frozen=True)
class Payment:
    date: date
    amount: Decimal


@dataclass(frozen=True)
class SpendingChange:
    action: str
    event_id: str
    new_amount: Decimal | None = None


@dataclass(frozen=True)
class ConfirmedCredit:
    date: date
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class ForecastContext:
    ledger: CanonicalLedger
    user_id: str
    request_date: date
    series: tuple[RecurringSeries, ...]
    evidence_report: dict[str, Any] | None = None
    confirmed_credits: tuple[ConfirmedCredit, ...] = ()


@dataclass(frozen=True)
class ForecastResult:
    dates: tuple[date, ...]
    balances: dict[date, Decimal]
    minimum_seen: Decimal
    min_date: date
    safe: bool
    first_violation: date | None
    credits: dict[date, Decimal]
    required_debits: dict[date, Decimal]
    proposed_payments: dict[date, Decimal]


def build_forecast_context(
    ledger: CanonicalLedger,
    *,
    user_id: str,
    request_date: date,
    evidence_report: dict[str, Any] | None = None,
) -> ForecastContext:
    series = infer_recurring_series(
        ledger,
        user_id=user_id,
        as_of=request_date,
        evidence_report=evidence_report,
    )
    confirmed: list[ConfirmedCredit] = []
    if evidence_report:
        for record in evidence_report.get("records", []):
            if not isinstance(record, dict) or record.get("user_id") != user_id:
                continue
            for fact in record.get("message_facts", []):
                if not isinstance(fact, dict) or fact.get("claim_type") != "confirm" or fact.get("status") != "confirmed":
                    continue
                # A targeted confirmation amends an existing ledger row.  The
                # ledger already contains that amended effect; only an
                # explicitly unscoped confirmation may create a one-off cash
                # inflow here.
                if fact.get("target_event_id") or fact.get("related_event_id"):
                    continue
                if fact.get("amount") is None or fact.get("effective_date") is None or fact.get("currency") is None:
                    continue
                try:
                    confirmed.append(ConfirmedCredit(
                        date.fromisoformat(str(fact["effective_date"])),
                        _decimal_value(fact["amount"]),
                        str(fact["currency"]).upper(),
                    ))
                except (TypeError, ValueError, ForecastError):
                    continue
    return ForecastContext(ledger, user_id, request_date, series, evidence_report, tuple(confirmed))


def _date_value(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ForecastError(f"invalid payment date: {value!r}")


def _decimal_value(value: Any) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ForecastError(f"invalid payment amount: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ForecastError(f"payment amount must be finite and non-negative: {value!r}")
    return parsed


def _normalise_payments(values: Iterable[Any]) -> tuple[Payment, ...]:
    result: list[Payment] = []
    for value in values:
        if isinstance(value, Payment):
            result.append(value)
        elif isinstance(value, dict):
            result.append(Payment(_date_value(value.get("date")), _decimal_value(value.get("amount"))))
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            result.append(Payment(_date_value(value[0]), _decimal_value(value[1])))
        else:
            raise ForecastError(f"invalid extra payment: {value!r}")
    return tuple(result)


def _normalise_changes(values: Iterable[Any]) -> tuple[SpendingChange, ...]:
    result: list[SpendingChange] = []
    for value in values:
        if isinstance(value, SpendingChange):
            result.append(value)
        elif isinstance(value, dict):
            result.append(SpendingChange(str(value.get("action")), str(value.get("event_id")), _decimal_value(value["new_amount"]) if value.get("new_amount") is not None else None))
        elif isinstance(value, str):
            parts = value.split(":")
            if len(parts) == 2 and parts[0] == "stop":
                result.append(SpendingChange("stop", parts[1]))
            elif len(parts) == 3 and parts[0] == "reduce_to":
                result.append(SpendingChange("reduce_to", parts[1], _decimal_value(parts[2])))
            else:
                raise ForecastError(f"invalid spending change: {value!r}")
        elif isinstance(value, (tuple, list)) and len(value) in {2, 3}:
            result.append(SpendingChange(str(value[0]), str(value[1]), _decimal_value(value[2]) if len(value) == 3 else None))
        else:
            raise ForecastError(f"invalid spending change: {value!r}")
    return tuple(result)


def _change_map(series: Iterable[RecurringSeries], changes: tuple[SpendingChange, ...]) -> dict[str, SpendingChange]:
    by_event: dict[str, RecurringSeries] = {}
    for row in series:
        for event_id in (*row.source_event_ids, row.output_event_id):
            by_event[event_id] = row
    selected: dict[str, SpendingChange] = {}
    for change in changes:
        row = by_event.get(change.event_id)
        if row is not None and row.direction == "debit":
            selected[row.series_id] = change
    return selected


def _converted_series_amount(context: ForecastContext, series: RecurringSeries, when: date) -> Decimal:
    rates = context.ledger.rates
    if series.currency == context.ledger.profiles[context.user_id].home_currency:
        return series.amount
    if rates is None:
        raise ForecastError(f"no rate book available for generated {series.currency} occurrence")
    return rates.convert_to_home_currency(
        series.amount,
        series.currency,
        context.ledger.profiles[context.user_id].home_currency,
        when,
    )


def _effect_series(effect: CashEffect, series_by_event: dict[str, RecurringSeries]) -> RecurringSeries | None:
    return series_by_event.get(effect.event_id)


def simulate(
    context: ForecastContext,
    extra_payments: Iterable[Any] = (),
    spending_changes: Iterable[Any] = (),
    horizon_end: date | None = None,
) -> ForecastResult:
    """Simulate opening balance through an inclusive horizon.

    Same-day ordering is deliberately visible here and locked to the project
    assumption: required debits, proposed payments, then confirmed credits.
    ``balances`` are end-of-day values; ``minimum_seen`` also observes the
    intermediate stages so a same-day credit cannot mask an earlier violation.
    """
    start = context.request_date
    end = horizon_end or (start + timedelta(days=89))
    if end < start:
        raise ForecastError("horizon_end precedes request_date")
    dates = tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    payment_rows = _normalise_payments(extra_payments)
    changes = _normalise_changes(spending_changes)
    selected_changes = _change_map(context.series, changes)
    series_by_event = {event_id: row for row in context.series for event_id in (*row.source_event_ids, row.output_event_id)}

    debits: defaultdict[date, Decimal] = defaultdict(Decimal)
    credits: defaultdict[date, Decimal] = defaultdict(Decimal)
    observed_series_dates: defaultdict[str, set[date]] = defaultdict(set)
    evidence_credit_dates = {row.date for row in context.confirmed_credits}
    for effect in context.ledger.effects_for_user(context.user_id):
        when = effect.effective_date
        if when is None or when < start or when > end or effect.amount is None:
            continue
        row = _effect_series(effect, series_by_event)
        if row is not None:
            observed_series_dates[row.series_id].add(when)
            if row.end_date is not None and when > row.end_date:
                continue
        amount = effect.amount
        if row is not None:
            change = selected_changes.get(row.series_id)
            if change is not None and change.action == "stop":
                continue
            if change is not None and change.action == "reduce_to" and change.new_amount is not None:
                amount = min(amount, change.new_amount)
        if effect.direction == "credit":
            credits[when] += amount
        elif effect.direction == "debit":
            debits[when] += amount

    # Validated confirmation facts can describe a future credit that has no
    # one-to-one event row (for example an approved invoice).  They are still
    # evidence, not a payment decision; only explicit amount/date/currency
    # facts enter this cash flow.
    for credit in context.confirmed_credits:
        if start <= credit.date <= end:
            rates = context.ledger.rates
            home_currency = context.ledger.profiles[context.user_id].home_currency
            if credit.currency == home_currency:
                amount = credit.amount
            elif rates is not None:
                amount = rates.convert_to_home_currency(credit.amount, credit.currency, home_currency, credit.date)
            else:
                raise ForecastError(f"no rate book available for evidence credit in {credit.currency}")
            credits[credit.date] += amount

    # Add projected recurrence legs only where an explicit ledger effect did
    # not already provide that occurrence.
    from .recurrence import VARIABLE_POOL_CATEGORIES
    active_income = any(
        row.direction == "credit"
        and row.category == "salary"
        and row.end_date is None
        for row in context.series
    ) or bool(context.confirmed_credits)
    for row in context.series:
        change = selected_changes.get(row.series_id)
        if change is not None and change.action == "stop":
            continue
        row_end = end
        # Variable spending pools assume income keeps arriving.  When every
        # salary stream has ended (seasonal contract finished, final payroll),
        # discretionary pool spending is only projected through a transition
        # window of about two months past its last observed occurrence instead
        # of the full horizon.
        if (
            not active_income
            and row.direction == "debit"
            and row.category in VARIABLE_POOL_CATEGORIES
            and row.occurrences
        ):
            row_end = min(end, max(row.occurrences) + timedelta(days=60))
        for when in projected_occurrences(row, start, row_end):
            if when in observed_series_dates[row.series_id]:
                continue
            # An explicit confirmed credit on the same date overrides a
            # generated salary occurrence, preventing an old estimate from
            # being counted alongside the amended/approved amount.
            if when in evidence_credit_dates and row.direction == "credit":
                continue
            amount = _converted_series_amount(context, row, when)
            if change is not None and change.action == "reduce_to" and change.new_amount is not None:
                amount = min(amount, change.new_amount)
            if row.direction == "credit":
                credits[when] += amount
            elif row.direction == "debit":
                debits[when] += amount

    payments: defaultdict[date, Decimal] = defaultdict(Decimal)
    for payment in payment_rows:
        if start <= payment.date <= end:
            payments[payment.date] += payment.amount

    profile = context.ledger.profiles[context.user_id]
    balance = profile.current_available_balance
    minimum_seen = balance
    min_date = start
    first_violation: date | None = start if balance < profile.minimum_balance_to_keep else None
    balances: dict[date, Decimal] = {}

    def observe(value: Decimal, when: date) -> None:
        nonlocal minimum_seen, min_date, first_violation
        if value < minimum_seen:
            minimum_seen, min_date = value, when
        if first_violation is None and value < profile.minimum_balance_to_keep:
            first_violation = when

    for when in dates:
        balance -= debits[when]
        observe(balance, when)
        balance += credits[when]
        observe(balance, when)
        # Proposed payments apply after required debits and confirmed credits:
        # a purchase made on payday may use the salary that lands that day.
        # The end-of-day balance must still hold the protected minimum.
        balance -= payments[when]
        observe(balance, when)
        balances[when] = balance
    return ForecastResult(
        dates=dates,
        balances=balances,
        minimum_seen=minimum_seen,
        min_date=min_date,
        safe=first_violation is None,
        first_violation=first_violation,
        credits={when: amount for when, amount in credits.items() if amount},
        required_debits={when: amount for when, amount in debits.items() if amount},
        proposed_payments={when: amount for when, amount in payments.items() if amount},
    )


def forecast(
    ledger: CanonicalLedger,
    *,
    user_id: str,
    request_date: date,
    extra_payments: Iterable[Any] = (),
    spending_changes: Iterable[Any] = (),
    horizon_end: date | None = None,
    evidence_report: dict[str, Any] | None = None,
) -> ForecastResult:
    context = build_forecast_context(ledger, user_id=user_id, request_date=request_date, evidence_report=evidence_report)
    return simulate(context, extra_payments, spending_changes, horizon_end)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key.isoformat() if isinstance(key, date) else key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def forecast_report(
    ledger: CanonicalLedger,
    requests: Iterable[dict[str, str]],
    *,
    evidence_report: dict[str, Any] | None = None,
    horizon_days: int = 90,
    request_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Build an auditable recurrence/forecast report without solving requests."""
    if horizon_days < 1:
        raise ForecastError("horizon_days must be positive")
    records: list[dict[str, Any]] = []
    for row in requests:
        if request_id and row.get("request_id") != request_id:
            continue
        if user_id and row.get("user_id") != user_id:
            continue
        current_user = row["user_id"]
        request_date = parse_date(row["request_date"])
        assert request_date is not None
        context = build_forecast_context(
            ledger,
            user_id=current_user,
            request_date=request_date,
            evidence_report=evidence_report,
        )
        result = simulate(
            context,
            horizon_end=request_date + timedelta(days=horizon_days - 1),
        )
        records.append({
            "request_id": row["request_id"],
            "user_id": current_user,
            "request_date": request_date,
            "horizon_end": request_date + timedelta(days=horizon_days - 1),
            "series": [asdict(item) for item in context.series],
            "forecast": asdict(result),
        })
    return {
        "forecast_version": "phase-3.v1",
        "forecast_days": horizon_days,
        "endpoint": "inclusive_request_date_plus_n_minus_1",
        "same_day_order": "required_debits_then_confirmed_credits_then_proposed_payments",
        "record_count": len(records),
        "records": _json_value(records),
    }


def write_forecast_report(report: dict[str, Any], output_path: Path = FORECAST_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Phase 3 recurrence and forecast report")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=FORECAST_OUTPUT)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--horizon-days", type=int, default=90)
    args = parser.parse_args(argv)
    evidence_path = args.evidence or EVIDENCE_OUTPUT
    report = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else None
    ledger = build_ledger_from_directory(args.dataset, evidence_path if evidence_path.is_file() else None)
    result = forecast_report(
        ledger,
        read_csv(Path(args.dataset) / "requests.csv"),
        evidence_report=report,
        horizon_days=args.horizon_days,
        request_id=args.request_id,
        user_id=args.user_id,
    )
    write_forecast_report(result, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "record_count": result["record_count"], "forecast_days": args.horizon_days}, sort_keys=True))
    return 0


__all__ = [
    "ForecastContext",
    "ForecastError",
    "ForecastResult",
    "ConfirmedCredit",
    "Payment",
    "SpendingChange",
    "build_forecast_context",
    "forecast",
    "forecast_report",
    "main",
    "simulate",
    "write_forecast_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
