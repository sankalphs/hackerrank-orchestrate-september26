"""Independent output validator for Phase 6.

The validator consumes the source dataset and canonical ledger directly.  It
does not trust the planner's candidate report; every emitted schedule is
parsed, checked against the request contract, and re-simulated.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

from .candidates import PaymentOption, _option_is_eligible, load_payment_options
from .capacity import capacity_for_request
from .config import OUTPUT_COLUMNS
from .forecast import ForecastContext, Payment, SpendingChange, build_forecast_context, simulate
from .loaders import parse_date
from .spending_changes import eligible_change_actions, spending_change_text


AFFORDABILITY_STATUSES = frozenset({"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"})
METHODS = frozenset({"full_payment", "partial_payment", "installments", "wait", "not_recommended"})


class ValidationError(ValueError):
    """Raised when an output violates a submission or safety invariant."""


@dataclass(frozen=True)
class ValidationReport:
    row_count: int
    validated_plan_count: int
    not_recommended_count: int


def _fail(request_id: str, message: str) -> None:
    raise ValidationError(f"{request_id}: {message}")


def _decimal(value: Any, field: str, request_id: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        _fail(request_id, f"{field} is not a valid decimal")
    if not parsed.is_finite() or parsed < 0:
        _fail(request_id, f"{field} must be finite and non-negative")
    return parsed


def _date(value: str, field: str, request_id: str, *, blank: bool = False) -> date | None:
    if blank and value.strip() == "":
        return None
    try:
        return parse_date(value)
    except (TypeError, ValueError):
        _fail(request_id, f"{field} must be YYYY-MM-DD")
    return None  # pragma: no cover


def _bool(value: str) -> bool:
    return value.strip().casefold() in {"1", "true", "yes", "y"}


def _parse_plan(value: str, request_id: str) -> tuple[Payment, ...]:
    if value == "none":
        return ()
    if not value.strip():
        _fail(request_id, "payment_plan is blank")
    payments: list[Payment] = []
    previous: date | None = None
    for entry in value.split("|"):
        parts = entry.split(":")
        if len(parts) != 2:
            _fail(request_id, f"invalid payment_plan entry: {entry!r}")
        when = _date(parts[0], "payment_plan date", request_id)
        amount = _decimal(parts[1], "payment_plan amount", request_id)
        if amount <= 0:
            _fail(request_id, "payment_plan amounts must be positive")
        if previous is not None and when < previous:
            _fail(request_id, "payment_plan is not chronological")
        payments.append(Payment(when, amount))
        previous = when
    return tuple(payments)


def _parse_changes(value: str, request_id: str) -> tuple[SpendingChange, ...]:
    if value == "none":
        return ()
    if not value.strip():
        _fail(request_id, "spending_changes_needed is blank")
    changes: list[SpendingChange] = []
    seen: set[str] = set()
    for entry in value.split("|"):
        parts = entry.split(":")
        if len(parts) == 2 and parts[0] == "stop":
            action = SpendingChange("stop", parts[1])
        elif len(parts) == 3 and parts[0] == "reduce_to":
            action = SpendingChange("reduce_to", parts[1], _decimal(parts[2], "reduction amount", request_id))
        else:
            _fail(request_id, f"invalid spending change: {entry!r}")
        if not action.event_id or action.event_id in seen:
            _fail(request_id, "spending changes contain a duplicate or blank event id")
        seen.add(action.event_id)
        changes.append(action)
    if len(changes) > 3:
        _fail(request_id, "at most three spending changes are allowed")
    return tuple(changes)


def _same_payments(left: Iterable[Payment], right: Iterable[Payment]) -> bool:
    left_rows, right_rows = tuple(left), tuple(right)
    return len(left_rows) == len(right_rows) and all(
        a.date == b.date and a.amount == b.amount for a, b in zip(left_rows, right_rows)
    )


def _matching_installment_option(
    options: Iterable[PaymentOption],
    payments: tuple[Payment, ...],
    *,
    request_date: date,
    deadline: date,
    max_months: int | None,
) -> PaymentOption | None:
    for option in options:
        if option.payment_method != "installments" or max_months is None:
            continue
        try:
            expected = option.payments()
        except Exception:
            continue
        if _option_is_eligible(option, request_date=request_date, deadline=deadline, max_months=max_months) and _same_payments(expected, payments):
            return option
    return None


class OutputValidator:
    """Validate serialized output against requests, ledger, and simulator."""

    def __init__(
        self,
        ledger,
        requests: Iterable[dict[str, str]],
        options_by_request: dict[str, tuple[PaymentOption, ...]] | None = None,
        *,
        evidence_report: dict[str, Any] | None = None,
        horizon_days: int = 90,
    ) -> None:
        if horizon_days < 1:
            raise ValidationError("horizon_days must be positive")
        self.ledger = ledger
        self.requests = tuple(requests)
        self.request_by_id = {row.get("request_id", ""): row for row in self.requests}
        if len(self.request_by_id) != len(self.requests):
            raise ValidationError("requests contain duplicate request_id values")
        self.options_by_request = options_by_request or {}
        self.evidence_report = evidence_report
        self.horizon_days = horizon_days

    def validate_rows(self, rows: Iterable[dict[str, str]]) -> ValidationReport:
        actual = [dict(row) for row in rows]
        expected_ids = [row.get("request_id", "") for row in self.requests]
        if any(tuple(row.keys()) != OUTPUT_COLUMNS for row in actual):
            raise ValidationError("output columns do not exactly match the required eight columns")
        actual_ids = [row.get("request_id", "") for row in actual]
        if actual_ids != expected_ids:
            raise ValidationError("output request_ids must match requests.csv exactly, in order")

        validated = 0
        not_recommended = 0
        for row in actual:
            request_id = row["request_id"]
            request = self.request_by_id[request_id]
            self._validate_row(row, request)
            if row["recommended_payment_method"] == "not_recommended":
                not_recommended += 1
            else:
                validated += 1
        return ValidationReport(len(actual), validated, not_recommended)

    def _validate_row(self, row: dict[str, str], request: dict[str, str]) -> None:
        request_id = request["request_id"]
        user_id = request["user_id"]
        if not row["request_id"]:
            _fail(request_id, "request_id is blank")
        profile = self.ledger.profiles.get(user_id)
        if profile is None:
            _fail(request_id, f"unknown user {user_id}")
        request_date = _date(request["request_date"], "request_date", request_id)
        deadline = _date(request["desired_completion_date"], "desired_completion_date", request_id)
        if deadline < request_date:
            _fail(request_id, "deadline precedes request date")
        requested = _decimal(request["requested_amount"], "requested_amount", request_id)
        safe = _decimal(row["amount_safe_to_pay"], "amount_safe_to_pay", request_id)
        if safe > requested:
            _fail(request_id, "amount_safe_to_pay exceeds requested amount")
        status = row["affordability_status"]
        method = row["recommended_payment_method"]
        if status not in AFFORDABILITY_STATUSES:
            _fail(request_id, f"unsupported affordability_status {status!r}")
        if method not in METHODS:
            _fail(request_id, f"unsupported recommended_payment_method {method!r}")
        if not row["decision_explanation"].strip():
            _fail(request_id, "decision_explanation is blank")

        capacity = capacity_for_request(
            self.ledger,
            request,
            evidence_report=self.evidence_report,
            horizon_days=self.horizon_days,
        )
        if safe != capacity.amount_safe_to_pay:
            _fail(request_id, f"amount_safe_to_pay does not match baseline capacity ({capacity.amount_safe_to_pay})")
        expected_earliest = capacity.earliest_date_for_full_payment
        actual_earliest = _date(row["earliest_date_for_full_payment"], "earliest_date_for_full_payment", request_id, blank=True)
        if actual_earliest != expected_earliest:
            _fail(request_id, f"earliest_date_for_full_payment does not match baseline capacity ({expected_earliest})")

        context = build_forecast_context(
            self.ledger,
            user_id=user_id,
            request_date=request_date,
            evidence_report=self.evidence_report,
        )
        payments = _parse_plan(row["payment_plan"], request_id)
        changes = _parse_changes(row["spending_changes_needed"], request_id)
        if spending_change_text(changes) != row["spending_changes_needed"]:
            _fail(request_id, "spending changes are not in canonical order")
        self._validate_changes(context, changes, request_id, deadline)
        accepted = set(profile.payment_methods_user_will_consider)

        if method == "not_recommended":
            if status != "not_affordable" or row["payment_plan"] != "none" or changes:
                _fail(request_id, "not_recommended must be not_affordable with no plan or changes")
            return
        if not payments:
            _fail(request_id, "recommended plans must not be none")
        if any(payment.date < request_date or payment.date > deadline for payment in payments):
            _fail(request_id, "payment plan is outside the request window")

        if method == "full_payment":
            if "full_payment" not in accepted or len(payments) != 1 or payments[0] != Payment(request_date, requested):
                _fail(request_id, "full payment plan is not an accepted exact payment")
            expected_status = "affordable_with_plan" if changes else "affordable_now"
        elif method == "partial_payment":
            if "partial_payment" not in accepted or not _bool(request.get("allows_partial_payment", "")):
                _fail(request_id, "partial payment is not permitted")
            if len(payments) != 2 or not (Decimal("0") < safe < requested):
                _fail(request_id, "partial payment must contain two legs and a non-trivial safe amount")
            if payments[0] != Payment(request_date, safe) or payments[1].amount != requested - safe:
                _fail(request_id, "partial payment legs do not match safe amount and requested total")
            if expected_earliest is None or expected_earliest > deadline or payments[1].date != expected_earliest:
                _fail(request_id, "partial payment second leg must use the baseline earliest date")
            expected_status = "affordable_with_plan"
        elif method == "installments":
            option = _matching_installment_option(
                self.options_by_request.get(request_id, ()),
                payments,
                request_date=request_date,
                deadline=deadline,
                max_months=profile.max_installment_months,
            )
            if "installments" not in accepted or option is None:
                _fail(request_id, "installment plan does not exactly match an accepted supplied option")
            expected_status = "affordable_with_plan"
        elif method == "wait":
            if "full_payment" not in accepted or len(payments) != 1 or payments[0].amount != requested or payments[0].date <= request_date:
                _fail(request_id, "wait must be a later accepted full payment")
            expected_status = "affordable_later"
        else:  # pragma: no cover - METHODS makes this unreachable
            _fail(request_id, f"unsupported method {method}")

        if status != expected_status:
            _fail(request_id, f"status {status!r} does not match method {method!r}")
        horizon_end = max(capacity.baseline_horizon_end, deadline, payments[-1].date)
        result = simulate(context, extra_payments=payments, spending_changes=changes, horizon_end=horizon_end)
        if not result.safe:
            _fail(request_id, f"emitted plan is unsafe; first violation is {result.first_violation}")

    def _validate_changes(self, context: ForecastContext, changes: tuple[SpendingChange, ...], request_id: str, deadline: date) -> None:
        horizon_end = max(context.request_date + timedelta(days=self.horizon_days - 1), deadline)
        actions = eligible_change_actions(context, horizon_end=horizon_end)
        by_key = {(action.event_id, action.action): action for action in actions}
        for change in changes:
            action = by_key.get((change.event_id, change.action))
            if action is None:
                _fail(request_id, f"spending change is not legal for {change.event_id}")
            if change.action == "reduce_to":
                if change.new_amount is None or change.new_amount >= action.current_amount:
                    _fail(request_id, f"reduction for {change.event_id} does not reduce current spending")
                if action.minimum_amount is not None and change.new_amount < action.minimum_amount:
                    _fail(request_id, f"reduction for {change.event_id} is below the permitted minimum")

    def validate_file(self, path: Path) -> ValidationReport:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ValidationError("output file is empty") from exc
            if tuple(header) != OUTPUT_COLUMNS:
                raise ValidationError("output header does not exactly match the required eight columns")
            values_rows = list(reader)
            for values in values_rows:
                if len(values) != len(OUTPUT_COLUMNS):
                    raise ValidationError("output contains a row with the wrong number of columns")
            rows = [dict(zip(OUTPUT_COLUMNS, values)) for values in values_rows]
            return self.validate_rows(rows)


def write_validated_output(
    rows: Iterable[dict[str, str]],
    output_path: Path,
    validator: Callable[[Path], ValidationReport],
) -> ValidationReport:
    """Write, validate, and atomically publish output while preserving old output on failure."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    materialized = [dict(row) for row in rows]
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(materialized)
        report = validator(temporary)
        os.replace(temporary, output_path)
        return report
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


__all__ = ["OutputValidator", "ValidationError", "ValidationReport", "load_payment_options", "write_validated_output"]
