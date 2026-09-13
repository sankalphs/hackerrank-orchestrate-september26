"""Phase 5 candidate generation, spending-change search, and selection."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .capacity import CapacityResult, capacity_for_context
from .config import DATASET_DIR, EVIDENCE_OUTPUT
from .event_resolution import build_ledger_from_directory
from .forecast import ForecastContext, Payment, SpendingChange, build_forecast_context, simulate
from .loaders import parse_date, read_csv
from .ranking import rank_candidates
from .spending_changes import search_safe_changes, spending_change_text


class CandidateError(ValueError):
    """Raised when a request or payment option cannot be evaluated safely."""


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Decimal
    total_payable_amount: Decimal

    def payments(self) -> tuple[Payment, ...]:
        if self.number_of_payments < 1:
            raise CandidateError(f"{self.payment_option_id}: number_of_payments must be positive")
        if self.number_of_payments == 1:
            return (Payment(self.first_payment_date, self.payment_amount),)
        if self.payment_frequency_days is None or self.payment_frequency_days <= 0:
            raise CandidateError(f"{self.payment_option_id}: installment frequency is missing")
        return tuple(
            Payment(
                self.first_payment_date + timedelta(days=index * self.payment_frequency_days),
                self.payment_amount,
            )
            for index in range(self.number_of_payments)
        )


@dataclass(frozen=True)
class PaymentCandidate:
    method: str
    status: str
    payments: tuple[Payment, ...]
    requested_amount: Decimal
    total_paid: Decimal
    spending_changes: tuple[SpendingChange, ...] = ()
    payment_option_id: str | None = None
    completes_by_deadline: bool = True
    rationale: str = ""

    @property
    def first_payment_date(self) -> date:
        return self.payments[0].date

    @property
    def payment_count(self) -> int:
        return len(self.payments)

    @property
    def completion_date(self) -> date:
        return self.payments[-1].date

    @property
    def canonical_signature(self) -> str:
        payments = "|".join(f"{row.date.isoformat()}:{row.amount}" for row in self.payments)
        changes = spending_change_text(self.spending_changes)
        return f"{self.method}|{self.payment_option_id or ''}|{payments}|{changes}"

    @property
    def payment_plan(self) -> str:
        # Payment-plan amounts keep two decimals when fractional, matching
        # the public sample format (620.40 stays padded, 25256 stays bare).
        return "|".join(
            f"{row.date.isoformat()}:{_plan_amount_text(row.amount)}"
            for row in self.payments
        )

    @property
    def spending_changes_needed(self) -> str:
        return spending_change_text(self.spending_changes)


@dataclass(frozen=True)
class DecisionPlan:
    request_id: str
    user_id: str
    request_date: date
    desired_completion_date: date
    requested_amount: Decimal
    currency: str
    amount_safe_to_pay: Decimal
    earliest_date_for_full_payment: date | None
    selected: PaymentCandidate
    candidates: tuple[PaymentCandidate, ...]
    candidate_horizon_end: date

    @property
    def payment_plan(self) -> str:
        return self.selected.payment_plan if self.selected.method != "not_recommended" else "none"


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise CandidateError(f"{field} is not a valid Decimal: {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise CandidateError(f"{field} must be finite and non-negative: {value!r}")
    return result


def _int(value: Any, field: str) -> int:
    try:
        result = int(str(value))
    except Exception as exc:
        raise CandidateError(f"{field} is not an integer: {value!r}") from exc
    return result


def _bool(value: Any) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def _plan_amount_text(value: Decimal) -> str:
    """Two decimals when fractional, bare integer otherwise (sample format)."""
    text = format(value, "f")
    whole, dot, fraction = text.partition(".")
    if not dot:
        return whole
    return f"{whole}.{fraction.ljust(2, '0')}"


def load_payment_options(dataset_dir: Path = DATASET_DIR) -> dict[str, tuple[PaymentOption, ...]]:
    options: dict[str, list[PaymentOption]] = {}
    for row in read_csv(Path(dataset_dir) / "request_payment_options.csv"):
        first = parse_date(row["first_payment_date"])
        assert first is not None
        frequency = row.get("payment_frequency_days", "").strip()
        option = PaymentOption(
            payment_option_id=row["payment_option_id"],
            request_id=row["request_id"],
            payment_method=row["payment_method"],
            payment_amount=_decimal(row["payment_amount"], "payment_amount"),
            number_of_payments=_int(row["number_of_payments"], "number_of_payments"),
            first_payment_date=first,
            payment_frequency_days=int(frequency) if frequency else None,
            financing_fee=_decimal(row["financing_fee"], "financing_fee"),
            total_payable_amount=_decimal(row["total_payable_amount"], "total_payable_amount"),
        )
        options.setdefault(option.request_id, []).append(option)
    return {key: tuple(value) for key, value in options.items()}


def _request_dates(request: dict[str, Any]) -> tuple[date, date]:
    request_date = parse_date(str(request["request_date"]))
    deadline = parse_date(str(request["desired_completion_date"]))
    if request_date is None or deadline is None or deadline < request_date:
        raise CandidateError(f"invalid request dates for {request.get('request_id')}")
    return request_date, deadline


def _option_is_eligible(option: PaymentOption, *, request_date: date, deadline: date, max_months: int | None) -> bool:
    if option.payment_method != "installments" or option.number_of_payments < 1:
        return False
    if option.first_payment_date < request_date:
        return False
    if max_months is None:
        return False
    try:
        payments = option.payments()
    except CandidateError:
        return False
    # ``max_installment_months`` limits elapsed calendar months, not the
    # number of installments.  A five-month option may legitimately contain
    # six payments when its first payment is followed monthly.
    absolute = option.first_payment_date.year * 12 + option.first_payment_date.month - 1 + max_months
    year, month_index = divmod(absolute, 12)
    month = month_index + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    month_end = (next_month - timedelta(days=1)).day
    max_end = date(year, month, min(option.first_payment_date.day, month_end))
    return payments[-1].date <= deadline and payments[-1].date <= max_end


def _candidate_horizon(capacity: CapacityResult, deadline: date, options: Iterable[PaymentOption]) -> date:
    # Candidate safety is evaluated through the requested completion window.
    # The separate Phase-4 baseline remains a 90-day metric and must not be
    # allowed to make a deadline-bounded candidate more conservative.  A
    # candidate still has to protect the same 90-day post-request commitments
    # as the baseline; installment legs can extend that horizon further.
    end = max(capacity.baseline_horizon_end, deadline)
    for option in options:
        try:
            # Options that cannot finish by the user's deadline are rejected
            # before simulation and must not inflate the forecast window.
            payments = option.payments()
            if payments[-1].date <= deadline:
                end = max(end, payments[-1].date)
        except CandidateError:
            continue
    return end


def _safe_payment_date(
    context: ForecastContext,
    *,
    amount: Decimal,
    start: date,
    end: date,
    horizon_end: date,
) -> date | None:
    for when in (start + timedelta(days=offset) for offset in range((end - start).days + 1)):
        if simulate(context, extra_payments=(Payment(when, amount),), horizon_end=horizon_end).safe:
            return when
    return None


def _add_full_candidates(
    result: list[PaymentCandidate],
    context: ForecastContext,
    *,
    request_date: date,
    deadline: date,
    requested: Decimal,
    horizon_end: date,
    accepted: set[str],
) -> None:
    if "full_payment" not in accepted:
        return
    payments = (Payment(request_date, requested),)
    if simulate(context, extra_payments=payments, horizon_end=horizon_end).safe:
        result.append(PaymentCandidate("full_payment", "affordable_now", payments, requested, requested, rationale="safe today"))
        return
    for changes in search_safe_changes(context, payments=payments, horizon_end=horizon_end):
        if not changes:
            continue
        if simulate(context, extra_payments=payments, spending_changes=changes, horizon_end=horizon_end).safe:
            result.append(PaymentCandidate("full_payment", "affordable_with_plan", payments, requested, requested, changes, rationale="safe today with permitted spending changes"))


def _add_partial_candidate(
    result: list[PaymentCandidate],
    context: ForecastContext,
    *,
    request: dict[str, Any],
    capacity: CapacityResult,
    requested: Decimal,
    request_date: date,
    deadline: date,
    horizon_end: date,
    accepted: set[str],
) -> None:
    if "partial_payment" not in accepted or not _bool(request.get("allows_partial_payment", "")):
        return
    safe = capacity.amount_safe_to_pay
    earliest = capacity.earliest_date_for_full_payment
    if not (Decimal("0") < safe < requested) or earliest is None or earliest > deadline:
        return
    payments = (Payment(request_date, safe), Payment(earliest, requested - safe))
    if simulate(context, extra_payments=payments, horizon_end=horizon_end).safe:
        result.append(PaymentCandidate("partial_payment", "affordable_with_plan", payments, requested, requested, rationale="two-leg partial payment"))


def _add_installment_candidates(
    result: list[PaymentCandidate],
    context: ForecastContext,
    *,
    options: Iterable[PaymentOption],
    request_date: date,
    deadline: date,
    horizon_end: date,
    requested: Decimal,
    accepted: set[str],
    max_months: int | None,
) -> None:
    if "installments" not in accepted or max_months is None:
        return
    for option in options:
        if not _option_is_eligible(option, request_date=request_date, deadline=deadline, max_months=max_months):
            continue
        payments = option.payments()
        if simulate(context, extra_payments=payments, horizon_end=horizon_end).safe:
            result.append(PaymentCandidate("installments", "affordable_with_plan", payments, requested, option.total_payable_amount, payment_option_id=option.payment_option_id, rationale="supplied installment option"))


def _add_wait_candidate(
    result: list[PaymentCandidate],
    context: ForecastContext,
    *,
    request_date: date,
    deadline: date,
    requested: Decimal,
    horizon_end: date,
    accepted: set[str],
) -> None:
    if "full_payment" not in accepted:
        return
    when = _safe_payment_date(context, amount=requested, start=request_date + timedelta(days=1), end=deadline, horizon_end=horizon_end)
    if when is not None:
        payments = (Payment(when, requested),)
        result.append(PaymentCandidate("wait", "affordable_later", payments, requested, requested, rationale="first later safe full-payment date"))


def plan_request(
    ledger,
    request: dict[str, Any],
    *,
    options: Iterable[PaymentOption] = (),
    evidence_report: dict[str, Any] | None = None,
    horizon_days: int = 90,
) -> DecisionPlan:
    if horizon_days < 1:
        raise CandidateError("horizon_days must be positive")
    request_date, deadline = _request_dates(request)
    requested = _decimal(request["requested_amount"], "requested_amount")
    context = build_forecast_context(ledger, user_id=str(request["user_id"]), request_date=request_date, evidence_report=evidence_report)
    capacity = capacity_for_context(context, requested_amount=requested, request_id=request.get("request_id"), horizon_days=horizon_days)
    profile = ledger.profiles[context.user_id]
    accepted = set(profile.payment_methods_user_will_consider)
    request_options = tuple(option for option in options if option.request_id == request.get("request_id"))
    horizon_end = _candidate_horizon(capacity, deadline, request_options)
    candidates: list[PaymentCandidate] = []
    _add_full_candidates(candidates, context, request_date=request_date, deadline=deadline, requested=requested, horizon_end=horizon_end, accepted=accepted)
    _add_partial_candidate(candidates, context, request=request, capacity=capacity, requested=requested, request_date=request_date, deadline=deadline, horizon_end=horizon_end, accepted=accepted)
    _add_installment_candidates(candidates, context, options=request_options, request_date=request_date, deadline=deadline, horizon_end=horizon_end, requested=requested, accepted=accepted, max_months=profile.max_installment_months)
    _add_wait_candidate(candidates, context, request_date=request_date, deadline=deadline, requested=requested, horizon_end=horizon_end, accepted=accepted)
    ranked = rank_candidates(candidates)
    if ranked:
        selected = ranked[0]
    else:
        selected = PaymentCandidate(
            "not_recommended", "not_affordable", (), requested, Decimal("0"),
            rationale="no accepted on-time schedule is safe",
        )
    # This is a baseline capacity field, not a deadline-filtered candidate
    # field.  Samples intentionally retain a date after the deadline (for
    # example request_06), so keep it exactly as computed by Phase 4.
    earliest = capacity.earliest_date_for_full_payment
    return DecisionPlan(
        request_id=str(request.get("request_id", "")),
        user_id=context.user_id,
        request_date=request_date,
        desired_completion_date=deadline,
        requested_amount=requested,
        currency=profile.home_currency,
        amount_safe_to_pay=capacity.amount_safe_to_pay,
        earliest_date_for_full_payment=earliest,
        selected=selected,
        candidates=ranked,
        candidate_horizon_end=horizon_end,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, PaymentCandidate):
        return _json_value({
            "method": value.method,
            "status": value.status,
            "payment_plan": value.payment_plan if value.method != "not_recommended" else "none",
            "total_paid": value.total_paid,
            "payment_count": value.payment_count,
            "first_payment_date": value.first_payment_date if value.payments else None,
            "completion_date": value.completion_date if value.payments else None,
            "payment_option_id": value.payment_option_id,
            "spending_changes_needed": value.spending_changes_needed,
            "completes_by_deadline": value.completes_by_deadline,
            "rationale": value.rationale,
            "canonical_signature": value.canonical_signature,
        })
    if isinstance(value, DecisionPlan):
        return _json_value({
            "request_id": value.request_id,
            "user_id": value.user_id,
            "request_date": value.request_date,
            "desired_completion_date": value.desired_completion_date,
            "requested_amount": value.requested_amount,
            "currency": value.currency,
            "amount_safe_to_pay": value.amount_safe_to_pay,
            "earliest_date_for_full_payment": value.earliest_date_for_full_payment,
            "candidate_horizon_end": value.candidate_horizon_end,
            "selected": value.selected,
            "candidates": value.candidates,
        })
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def plan_report(
    ledger,
    requests: Iterable[dict[str, Any]],
    options_by_request: dict[str, tuple[PaymentOption, ...]],
    *,
    evidence_report: dict[str, Any] | None = None,
    horizon_days: int = 90,
    request_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    plans: list[DecisionPlan] = []
    for request in requests:
        if request_id and request.get("request_id") != request_id:
            continue
        if user_id and request.get("user_id") != user_id:
            continue
        plans.append(plan_request(ledger, request, options=options_by_request.get(request.get("request_id", ""), ()), evidence_report=evidence_report, horizon_days=horizon_days))
    return {
        "candidate_version": "phase-5.v1",
        "forecast_days": horizon_days,
        "record_count": len(plans),
        "records": _json_value(plans),
    }


def write_plan_report(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(_json_value(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Phase 5 candidate report")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--horizon-days", type=int, default=90)
    args = parser.parse_args(argv)
    evidence_path = args.evidence or EVIDENCE_OUTPUT
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else None
    ledger = build_ledger_from_directory(args.dataset, evidence_path if evidence_path.is_file() else None)
    report = plan_report(
        ledger,
        read_csv(Path(args.dataset) / "requests.csv"),
        load_payment_options(args.dataset),
        evidence_report=evidence,
        horizon_days=args.horizon_days,
        request_id=args.request_id,
        user_id=args.user_id,
    )
    write_plan_report(report, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "record_count": report["record_count"], "forecast_days": args.horizon_days}, sort_keys=True))
    return 0


__all__ = [
    "CandidateError",
    "DecisionPlan",
    "PaymentCandidate",
    "PaymentOption",
    "load_payment_options",
    "main",
    "plan_report",
    "plan_request",
    "write_plan_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
