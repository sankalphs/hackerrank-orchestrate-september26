"""Deterministic decision traces and explanation templates for Phase 6."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .candidates import DecisionPlan, PaymentCandidate
from .forecast import ForecastResult, build_forecast_context, simulate
from .spending_changes import spending_change_text


@dataclass(frozen=True)
class DecisionTrace:
    """All facts an explanation may use, derived from the financial engine."""

    request_id: str
    user_id: str
    currency: str
    request_date: date
    desired_completion_date: date
    requested_amount: Decimal
    amount_safe_to_pay: Decimal
    minimum_balance_to_keep: Decimal
    opening_balance: Decimal
    earliest_date_for_full_payment: date | None
    method: str
    status: str
    payments: tuple[Any, ...]
    total_paid: Decimal
    spending_changes: tuple[Any, ...]
    selected_forecast: ForecastResult
    provenance: tuple[str, ...] = ()

    @property
    def minimum_after_plan(self) -> Decimal:
        return self.selected_forecast.minimum_seen


def build_decision_trace(
    plan: DecisionPlan,
    ledger,
    *,
    evidence_report: dict[str, Any] | None = None,
) -> DecisionTrace:
    """Build a trace by re-running the selected schedule through the simulator."""
    profile = ledger.profiles[plan.user_id]
    context = build_forecast_context(
        ledger,
        user_id=plan.user_id,
        request_date=plan.request_date,
        evidence_report=evidence_report,
    )
    baseline_end = plan.request_date + timedelta(days=89)
    horizon_end = max(plan.candidate_horizon_end, baseline_end, plan.desired_completion_date)
    selected = plan.selected
    selected_forecast = simulate(
        context,
        extra_payments=selected.payments,
        spending_changes=selected.spending_changes,
        horizon_end=horizon_end,
    )
    return DecisionTrace(
        request_id=plan.request_id,
        user_id=plan.user_id,
        currency=plan.currency,
        request_date=plan.request_date,
        desired_completion_date=plan.desired_completion_date,
        requested_amount=plan.requested_amount,
        amount_safe_to_pay=plan.amount_safe_to_pay,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        opening_balance=profile.current_available_balance,
        earliest_date_for_full_payment=plan.earliest_date_for_full_payment,
        method=selected.method,
        status=selected.status,
        payments=selected.payments,
        total_paid=selected.total_paid,
        spending_changes=selected.spending_changes,
        selected_forecast=selected_forecast,
        provenance=("profile", "canonical_ledger", "forecast", "payment_options"),
    )


def _amount(value: Decimal) -> str:
    # Explanation money shows two decimals when fractional (620.40, 3,246.10)
    # and stays bare for integers (25,256).
    whole, dot, fraction = format(value, "f").partition(".")
    whole = f"{int(whole):,}"
    if not dot:
        return whole
    return f"{whole}.{fraction.ljust(2, '0')}"


def _money(currency: str, value: Decimal) -> str:
    return f"{currency} {_amount(value)}"


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _long_date(value: date) -> str:
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def _plan_amount(value: Decimal) -> str:
    """Payment-plan amounts show two decimals when fractional, bare otherwise."""
    text = format(value, "f")
    whole, dot, fraction = text.partition(".")
    if not dot:
        return whole
    return f"{whole}.{fraction.ljust(2, '0')}"


def _change_phrase(trace: DecisionTrace, ledger) -> str:
    phrases: list[str] = []
    for change in trace.spending_changes:
        event = ledger.events.get(change.event_id)
        name = event.description if event is not None else change.event_id
        if change.action == "stop":
            phrases.append(f"Stop the {name.lower()}")
        else:
            phrases.append(f"Reduce the {name.lower()} to {_money(trace.currency, change.new_amount or Decimal('0'))}")
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2:
        return f"{phrases[0]} and {phrases[1]}"
    return ", ".join(phrases[:-1]) + f", and {phrases[-1]}"


def explain_decision(trace: DecisionTrace, ledger) -> str:
    """Render a concise explanation using only facts present in ``trace``."""
    requested = _money(trace.currency, trace.requested_amount)
    # Explanations quote the user's protected minimum, matching the public
    # sample wording style.
    minimum = _money(trace.currency, trace.minimum_balance_to_keep)
    selected = trace.payments

    if trace.method == "not_recommended":
        return (
            f"Do not make this payment by {_long_date(trace.desired_completion_date)}. "
            f"None of the available options keeps the {minimum} minimum protected."
        )
    if trace.method == "full_payment":
        if trace.spending_changes:
            return (
                f"{_change_phrase(trace, ledger)}, then pay {requested} today. "
                f"This leaves at least {minimum} available."
            )
        return (
            f"Pay {requested} today. "
            f"This leaves at least {minimum} available over the next 90 days."
        )
    if trace.method == "partial_payment":
        first, second = selected
        return (
            f"Pay {_money(trace.currency, first.amount)} today and the remaining "
            f"{_money(trace.currency, second.amount)} on {_long_date(second.date)}. "
            f"This completes the full request and keeps the {minimum} minimum protected."
        )
    if trace.method == "installments":
        amounts = {payment.amount for payment in selected}
        if len(amounts) == 1:
            detail = f"of {_money(trace.currency, selected[0].amount)}"
        else:
            detail = "as supplied"
        return (
            f"Use {len(selected)} installments {detail}, starting {_long_date(selected[0].date)}. "
            f"This leaves at least {minimum} available."
        )
    if trace.method == "wait":
        when = selected[0].date
        if (when - trace.request_date).days < 14:
            # A short delay reads as an instruction to hold briefly; a long
            # one as a later full-payment date.
            return (
                f"Wait until {_long_date(when)}, then pay {requested} in full. "
                f"Paying sooner would put the {minimum} minimum at risk."
            )
        return (
            f"Pay {requested} in full on {_long_date(when)}. "
            f"Paying earlier would take the balance below the {minimum} minimum."
        )
    raise ValueError(f"unsupported decision method: {trace.method}")


def output_row(plan: DecisionPlan, trace: DecisionTrace, ledger) -> dict[str, str]:
    """Convert a plan and validated trace into the exact submission schema."""
    return {
        "request_id": plan.request_id,
        "amount_safe_to_pay": format(plan.amount_safe_to_pay, "f"),
        "affordability_status": plan.selected.status,
        "recommended_payment_method": plan.selected.method,
        "payment_plan": plan.payment_plan,
        "earliest_date_for_full_payment": (
            plan.earliest_date_for_full_payment.isoformat()
            if plan.earliest_date_for_full_payment is not None else ""
        ),
        "spending_changes_needed": spending_change_text(plan.selected.spending_changes),
        "decision_explanation": explain_decision(trace, ledger),
    }


__all__ = ["DecisionTrace", "build_decision_trace", "explain_decision", "output_row"]
