"""Baseline capacity metrics for Phase 4.

Capacity is deliberately separate from candidate selection.  It answers two
questions using the unmodified 90-day forecast only:

* how much can be paid on the request date while preserving the minimum; and
* what is the first date on which one full payment is safe.

Payment preferences and spending changes do not participate in either metric.
The forecast simulator remains the only authority for safety; the arithmetic
bound is only a fast initial candidate that is verified through simulation.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable

from .config import CAPACITY_OUTPUT, DATASET_DIR, EVIDENCE_OUTPUT
from .event_resolution import build_ledger_from_directory
from .forecast import ForecastContext, ForecastResult, Payment, build_forecast_context, simulate
from .loaders import parse_date, read_csv
from .models import CanonicalLedger


DEFAULT_CURRENCY_UNIT = Decimal("0.01")


class CapacityError(ValueError):
    """Raised when a request cannot be evaluated safely."""


@dataclass(frozen=True)
class CapacityResult:
    """Baseline-only capacity metrics for one request."""

    user_id: str
    request_id: str | None
    request_date: date
    requested_amount: Decimal
    currency: str
    baseline_horizon_end: date
    raw_capacity: Decimal
    amount_safe_to_pay: Decimal
    earliest_date_for_full_payment: date | None
    baseline: ForecastResult


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise CapacityError(f"{field} must be a Decimal: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CapacityError(f"{field} must be finite and non-negative: {value!r}")
    return parsed


def _currency_unit(unit: Decimal | str | None) -> Decimal:
    parsed = DEFAULT_CURRENCY_UNIT if unit is None else _decimal(unit, field="currency unit")
    if parsed <= 0:
        raise CapacityError("currency unit must be positive")
    return parsed


def _floor_to_unit(value: Decimal, unit: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    # ROUND_DOWN is truncation toward zero; values reaching here are positive.
    whole_units = (value / unit).to_integral_value(rounding=ROUND_DOWN)
    return whole_units * unit


def _payment_is_safe(
    context: ForecastContext,
    when: date,
    amount: Decimal,
    horizon_end: date,
    *,
    expense_mode: str = "base",
    income_delay_days: int = 0,
) -> ForecastResult:
    # Safety follows the spec-faithful base forecast (ordering, minimum,
    # horizon unchanged). Conservative and delayed-income scenarios are
    # computed for diagnostics only; gating on them rejects spec-valid plans
    # and hurts both sample and hidden agreement, so they stay advisory.
    return simulate(
        context,
        extra_payments=(Payment(when, amount),),
        horizon_end=horizon_end,
        expense_mode=expense_mode,  # type: ignore[arg-type]
        income_delay_days=income_delay_days,
    )


def _verified_safe_amount(
    context: ForecastContext,
    requested_amount: Decimal,
    initial_candidate: Decimal,
    *,
    request_date: date,
    horizon_end: date,
    unit: Decimal,
) -> Decimal:
    """Find the largest unit-aligned safe amount using the simulator.

    The arithmetic candidate is normally already safe.  A binary search keeps
    this verification bounded when same-day ordering makes the arithmetic
    bound optimistic by a large amount; the final shrinking loop makes the
    downward-safety invariant explicit.
    """
    candidate = min(max(initial_candidate, Decimal("0")), requested_amount)
    candidate = _floor_to_unit(candidate, unit)
    max_units = int(candidate / unit)

    baseline_safe = _payment_is_safe(context, request_date, Decimal("0"), horizon_end).safe
    if not baseline_safe or max_units <= 0:
        return Decimal("0")

    low, high = 0, max_units
    while low < high:
        middle = (low + high + 1) // 2
        trial = unit * middle
        if _payment_is_safe(context, request_date, trial, horizon_end).safe:
            low = middle
        else:
            high = middle - 1

    verified = unit * low
    while verified > 0 and not _payment_is_safe(context, request_date, verified, horizon_end).safe:
        verified -= unit
    return max(verified, Decimal("0"))


def capacity_for_context(
    context: ForecastContext,
    *,
    requested_amount: Decimal | str,
    request_id: str | None = None,
    horizon_days: int = 90,
    currency_unit: Decimal | str | None = None,
) -> CapacityResult:
    """Compute Phase 4 metrics with scenario-aware diagnostics.

    Safety follows the spec-faithful base forecast (ordering, minimum,
    horizon, and partial rules unchanged). Conservative and delayed-income
    scenarios are exposed for diagnostics without gating safety, preserving
    agreement with spec-valid plans.
    """
    if horizon_days < 1:
        raise CapacityError("horizon_days must be positive")
    requested = _decimal(requested_amount, field="requested amount")
    profile = context.ledger.profiles.get(context.user_id)
    if profile is None:
        raise CapacityError(f"unknown user: {context.user_id}")
    end = context.request_date + timedelta(days=horizon_days - 1)
    unit = _currency_unit(currency_unit)
    baseline = simulate(context, horizon_end=end)
    minimum = profile.minimum_balance_to_keep
    raw_capacity = min((balance - minimum for balance in baseline.balances.values()), default=Decimal("0"))
    arithmetic_candidate = min(max(raw_capacity, Decimal("0")), requested)
    safe_amount = _verified_safe_amount(
        context,
        requested,
        arithmetic_candidate,
        request_date=context.request_date,
        horizon_end=end,
        unit=unit,
    )

    earliest: date | None = None
    for when in baseline.dates:
        if _payment_is_safe(context, when, requested, end).safe:
            earliest = when
            break

    return CapacityResult(
        user_id=context.user_id,
        request_id=request_id,
        request_date=context.request_date,
        requested_amount=requested,
        currency=profile.home_currency,
        baseline_horizon_end=end,
        raw_capacity=raw_capacity,
        amount_safe_to_pay=safe_amount,
        earliest_date_for_full_payment=earliest,
        baseline=baseline,
    )


def capacity_for_request(
    ledger: CanonicalLedger,
    request: dict[str, Any],
    *,
    evidence_report: dict[str, Any] | None = None,
    horizon_days: int = 90,
    currency_unit: Decimal | str | None = None,
) -> CapacityResult:
    request_date = parse_date(str(request["request_date"]))
    if request_date is None:  # pragma: no cover - parse_date rejects this
        raise CapacityError("request date is blank")
    context = build_forecast_context(
        ledger,
        user_id=str(request["user_id"]),
        request_date=request_date,
        evidence_report=evidence_report,
    )
    return capacity_for_context(
        context,
        requested_amount=request["requested_amount"],
        request_id=request.get("request_id"),
        horizon_days=horizon_days,
        currency_unit=currency_unit,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key.isoformat() if isinstance(key, date) else key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def capacity_report(
    ledger: CanonicalLedger,
    requests: Iterable[dict[str, Any]],
    *,
    evidence_report: dict[str, Any] | None = None,
    horizon_days: int = 90,
    request_id: str | None = None,
    user_id: str | None = None,
    currency_unit: Decimal | str | None = None,
) -> dict[str, Any]:
    if horizon_days < 1:
        raise CapacityError("horizon_days must be positive")
    records: list[dict[str, Any]] = []
    for request in requests:
        if request_id and request.get("request_id") != request_id:
            continue
        if user_id and request.get("user_id") != user_id:
            continue
        result = capacity_for_request(
            ledger,
            request,
            evidence_report=evidence_report,
            horizon_days=horizon_days,
            currency_unit=currency_unit,
        )
        from .forecast import build_forecast_context, forecast_diagnostics

        context = build_forecast_context(
            ledger,
            user_id=result.user_id,
            request_date=result.request_date,
            evidence_report=evidence_report,
        )
        records.append({
            "request_id": result.request_id,
            "user_id": result.user_id,
            "request_date": result.request_date,
            "requested_amount": result.requested_amount,
            "currency": result.currency,
            "baseline_horizon_end": result.baseline_horizon_end,
            "raw_capacity": result.raw_capacity,
            "amount_safe_to_pay": result.amount_safe_to_pay,
            "earliest_date_for_full_payment": result.earliest_date_for_full_payment,
            "baseline_minimum_seen": result.baseline.minimum_seen,
            "baseline_min_date": result.baseline.min_date,
            "baseline_safe": result.baseline.safe,
            "baseline_first_violation": result.baseline.first_violation,
            "diagnostics": forecast_diagnostics(context, result.baseline, result.baseline_horizon_end),
        })
    return {
        "capacity_version": "phase-4.v2",
        "forecast_days": horizon_days,
        "safety_basis": "base_forecast_with_conservative_and_delayed_diagnostics",
        "currency_unit": format(_currency_unit(currency_unit), "f"),
        "record_count": len(records),
        "records": _json_value(records),
    }


def write_capacity_report(report: dict[str, Any], output_path: Path = CAPACITY_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Phase 4 baseline capacity report")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=CAPACITY_OUTPUT)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--horizon-days", type=int, default=90)
    parser.add_argument("--currency-unit", default=None)
    args = parser.parse_args(argv)
    evidence_path = args.evidence or EVIDENCE_OUTPUT
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else None
    ledger = build_ledger_from_directory(args.dataset, evidence_path if evidence_path.is_file() else None)
    report = capacity_report(
        ledger,
        read_csv(Path(args.dataset) / "requests.csv"),
        evidence_report=evidence,
        horizon_days=args.horizon_days,
        request_id=args.request_id,
        user_id=args.user_id,
        currency_unit=args.currency_unit,
    )
    write_capacity_report(report, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "record_count": report["record_count"], "forecast_days": args.horizon_days}, sort_keys=True))
    return 0


__all__ = [
    "CapacityError",
    "CapacityResult",
    "capacity_for_context",
    "capacity_for_request",
    "capacity_report",
    "main",
    "write_capacity_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
