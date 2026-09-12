"""Shared paths, schemas, vocabularies, and locked Phase 0 assumptions.

This module intentionally contains no solver logic.  Keeping the assumptions in
one place makes later calibration changes explicit and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = PROJECT_ROOT / "dataset"
AUDIT_OUTPUT = PROJECT_ROOT / "code" / "evaluation" / "input_audit.json"
CACHE_DIR = PROJECT_ROOT / "code" / "cache"
EVIDENCE_OUTPUT = PROJECT_ROOT / "code" / "evaluation" / "evidence_report.json"
LEDGER_OUTPUT = PROJECT_ROOT / "code" / "evaluation" / "ledger_report.json"
FORECAST_OUTPUT = PROJECT_ROOT / "code" / "evaluation" / "forecast_report.json"
PROMPT_VERSION = "phase-1.v1"

OUTPUT_COLUMNS = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)

DATA_FILES = (
    "financial_profiles.csv",
    "financial_events.csv",
    "exchange_rates.csv",
    "requests.csv",
    "sample_requests.csv",
    "request_payment_options.csv",
    "messages.csv",
    "images.csv",
    "output.csv",
)

EXPECTED_SCHEMAS = {
    "requests.csv": (
        "request_id", "user_id", "request_date", "request_type",
        "requested_amount", "desired_completion_date", "allows_partial_payment",
        "request_text",
    ),
    "sample_requests.csv": (
        "request_id", "user_id", "request_date", "request_type",
        "requested_amount", "desired_completion_date", "allows_partial_payment",
        "request_text", *OUTPUT_COLUMNS[1:],
    ),
    "financial_profiles.csv": (
        "user_id", "home_currency", "current_available_balance",
        "minimum_balance_to_keep", "financial_priorities",
        "expense_categories_to_protect",
        "expense_categories_user_is_willing_to_reduce",
        "expense_categories_user_is_willing_to_stop",
        "payment_methods_user_will_consider", "max_installment_months",
    ),
    "financial_events.csv": (
        "event_id", "user_id", "event_type", "description", "category",
        "direction", "amount", "currency", "event_date", "settlement_date",
        "status", "linked_event_id", "flexibility", "minimum_allowed_amount",
    ),
    "exchange_rates.csv": ("rate_date", "from_currency", "to_currency", "rate"),
    "request_payment_options.csv": (
        "payment_option_id", "request_id", "payment_method", "payment_amount",
        "number_of_payments", "first_payment_date", "payment_frequency_days",
        "financing_fee", "total_payable_amount",
    ),
    "messages.csv": (
        "message_id", "user_id", "request_id", "related_event_id", "sent_at",
        "source_type", "message_text",
    ),
    "images.csv": ("image_id", "user_id", "request_id", "related_event_id"),
    "output.csv": OUTPUT_COLUMNS,
}

SUPPORTED_CURRENCIES = frozenset({"EUR", "IDR", "INR", "USD", "ZAR"})
REQUEST_TYPES = frozenset({
    "purchase", "travel", "education", "family_transfer", "debt_repayment",
    "investment", "housing", "emergency_expense", "other",
})
EVENT_TYPES = frozenset({
    "debt_payment", "expense", "income", "investment_purchase",
    "investment_sale", "investment_valuation", "refund", "subscription",
})
DIRECTIONS = frozenset({"credit", "debit", "non_cash"})
STATUSES = frozenset({"cancelled", "failed", "pending", "scheduled", "settled", "unrealized"})
FLEXIBILITIES = frozenset({"fixed", "stoppable", "reducible", "reducible_or_stoppable"})
PAYMENT_METHODS = frozenset({"full_payment", "installments", "partial_payment"})
SOURCE_TYPES = frozenset({"bank", "employer", "financial_service", "merchant", "service_provider"})


@dataclass(frozen=True)
class Assumptions:
    """Decisions needed by the financial engine, locked before Phase 1."""

    forecast_days: int = 90
    forecast_endpoint: str = "inclusive_request_date_plus_89"
    balance_anchor: str = "profile_as_of_request"
    cash_flow_date: str = "settlement_date_when_present_else_event_date"
    fx_date: str = "exact_settlement_date"
    fx_direction: str = "from_event_currency_to_home_currency"
    amount_sign: str = "direction_controls_sign"
    same_day_order: str = "required_debits_then_proposed_payments_then_confirmed_credits"
    recurrence_inference: str = "explicit_metadata_then_validated_evidence_then_stable_cadence"
    unresolved_blank_amount: str = "block_dependent_cash_flow_never_zero"


ASSUMPTIONS = Assumptions()
