"""Phase 0 input audit for the Buy or Wait? dataset.

The audit is deliberately deterministic and dependency-free.  It does not make
affordability decisions; it reports structural issues and observations needed by
the later ledger and evidence stages.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from .config import (
    ASSUMPTIONS,
    AUDIT_OUTPUT,
    DATA_FILES,
    DATASET_DIR,
    DIRECTIONS,
    EVENT_TYPES,
    EXPECTED_SCHEMAS,
    FLEXIBILITIES,
    PAYMENT_METHODS,
    REQUEST_TYPES,
    SOURCE_TYPES,
    STATUSES,
    SUPPORTED_CURRENCIES,
)


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LIST_FIELDS = (
    "financial_priorities",
    "expense_categories_to_protect",
    "expense_categories_user_is_willing_to_reduce",
    "expense_categories_user_is_willing_to_stop",
    "payment_methods_user_will_consider",
)


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def _scale(value: str) -> int | None:
    if _blank(value):
        return None
    try:
        return max(0, -Decimal(value.strip()).as_tuple().exponent)
    except InvalidOperation:
        return None


def _parse_decimal(value: str, *, allow_blank: bool = False) -> Decimal | None:
    if _blank(value):
        if allow_blank:
            return None
        raise InvalidOperation("blank")
    return Decimal(value.strip())


def _parse_date(value: str, *, allow_blank: bool = False) -> date | None:
    if _blank(value):
        if allow_blank:
            return None
        raise ValueError("blank")
    if not DATE_RE.fullmatch(value.strip()):
        raise ValueError("not YYYY-MM-DD")
    return date.fromisoformat(value.strip())


def _parse_datetime(value: str) -> datetime:
    if not DATETIME_RE.fullmatch(value.strip()):
        raise ValueError("not UTC ISO-8601")
    return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class DatasetAudit:
    def __init__(self, dataset_dir: Path):
        self.dataset_dir = dataset_dir.resolve()
        self.rows: dict[str, list[dict[str, str]]] = {}
        self.issues: dict[str, list[object]] = defaultdict(list)
        self.observed: dict[str, object] = {}
        self.file_info: dict[str, object] = {}

    def issue(self, category: str, value: object) -> None:
        self.issues[category].append(value)

    def load(self) -> None:
        for name in DATA_FILES:
            path = self.dataset_dir / name
            if not path.is_file():
                self.issue("missing_files", name)
                self.file_info[name] = {"exists": False, "row_count": 0}
                continue
            try:
                rows = _rows(path)
            except (OSError, csv.Error) as exc:
                self.issue("unreadable_files", {"file": name, "error": str(exc)})
                self.file_info[name] = {"exists": True, "row_count": 0}
                continue
            self.rows[name] = rows
            headers = list(rows[0]) if rows else []
            expected = EXPECTED_SCHEMAS[name]
            if tuple(headers) != tuple(expected):
                self.issue("schema_errors", {
                    "file": name, "expected": list(expected), "actual": headers,
                })
            self.file_info[name] = {
                "exists": True,
                "row_count": len(rows),
                "headers": headers,
            }

    def _check_duplicates(self, name: str, key: str) -> None:
        rows = self.rows.get(name, [])
        values = [r.get(key, "") for r in rows if not _blank(r.get(key))]
        duplicates = sorted(k for k, n in Counter(values).items() if n > 1)
        if duplicates:
            self.issue("duplicate_ids", {"file": name, "field": key, "values": duplicates})

    def _check_dates(self, name: str, fields: dict[str, bool], *, datetime_fields: set[str] = set()) -> None:
        for row_number, row in enumerate(self.rows.get(name, []), start=2):
            for field, allow_blank in fields.items():
                value = row.get(field, "")
                try:
                    if field in datetime_fields:
                        if _blank(value) and allow_blank:
                            continue
                        _parse_datetime(value)
                    else:
                        _parse_date(value, allow_blank=allow_blank)
                except ValueError as exc:
                    self.issue("malformed_dates", {
                        "file": name, "row": row_number, "field": field,
                        "value": value, "error": str(exc),
                    })

    def _check_decimal_fields(self, name: str, fields: dict[str, bool]) -> None:
        for row_number, row in enumerate(self.rows.get(name, []), start=2):
            for field, allow_blank in fields.items():
                value = row.get(field, "")
                try:
                    parsed = _parse_decimal(value, allow_blank=allow_blank)
                    if parsed is not None and parsed < 0:
                        self.issue("negative_amounts", {
                            "file": name, "row": row_number, "field": field, "value": value,
                        })
                except InvalidOperation:
                    self.issue("malformed_amounts", {
                        "file": name, "row": row_number, "field": field, "value": value,
                    })

    def _check_enums(self, name: str, field: str, allowed: set[str] | frozenset[str]) -> None:
        observed = sorted({r.get(field, "") for r in self.rows.get(name, [])})
        bad = [value for value in observed if value not in allowed]
        if bad:
            self.issue("unsupported_enums", {"file": name, "field": field, "values": bad})
        self.observed[f"{name}.{field}"] = observed

    def _check_foreign_keys(self) -> None:
        profiles = self.rows.get("financial_profiles.csv", [])
        events = self.rows.get("financial_events.csv", [])
        requests = self.rows.get("requests.csv", [])
        samples = self.rows.get("sample_requests.csv", [])
        options = self.rows.get("request_payment_options.csv", [])
        messages = self.rows.get("messages.csv", [])
        images = self.rows.get("images.csv", [])

        profile_users = {r.get("user_id", "") for r in profiles}
        event_ids = {r.get("event_id", "") for r in events}
        request_ids = {r.get("request_id", "") for r in requests + samples}
        option_ids = {r.get("payment_option_id", "") for r in options}

        for name, rows, field, allowed, allow_blank in (
            ("requests.csv", requests, "user_id", profile_users, False),
            ("financial_events.csv", events, "user_id", profile_users, False),
            ("request_payment_options.csv", options, "request_id", request_ids, False),
            ("messages.csv", messages, "user_id", profile_users, False),
            ("messages.csv", messages, "request_id", request_ids, True),
            ("messages.csv", messages, "related_event_id", event_ids, True),
            ("images.csv", images, "user_id", profile_users, False),
            ("images.csv", images, "request_id", request_ids, False),
            ("images.csv", images, "related_event_id", event_ids, False),
        ):
            for row_number, row in enumerate(rows, start=2):
                value = row.get(field, "")
                if allow_blank and _blank(value):
                    continue
                if value not in allowed:
                    self.issue("invalid_foreign_keys", {
                        "file": name, "row": row_number, "field": field, "value": value,
                    })

        for row_number, row in enumerate(events, start=2):
            linked = row.get("linked_event_id", "")
            if linked and linked not in event_ids:
                self.issue("invalid_foreign_keys", {
                    "file": "financial_events.csv", "row": row_number,
                    "field": "linked_event_id", "value": linked,
                })
            if linked and linked in event_ids:
                parent = next(e for e in events if e.get("event_id") == linked)
                if parent.get("user_id") != row.get("user_id"):
                    self.issue("cross_user_links", {
                        "file": "financial_events.csv", "row": row_number,
                        "event_id": row.get("event_id"), "linked_event_id": linked,
                    })

        for name, rows, key in (
            ("requests.csv", requests, "request_id"),
            ("sample_requests.csv", samples, "request_id"),
            ("request_payment_options.csv", options, "payment_option_id"),
            ("messages.csv", messages, "message_id"),
            ("images.csv", images, "image_id"),
        ):
            self._check_duplicates(name, key)
        self._check_duplicates("financial_profiles.csv", "user_id")
        self._check_duplicates("financial_events.csv", "event_id")

        self.observed["reference_counts"] = {
            "profile_users": len(profile_users),
            "event_ids": len(event_ids),
            "request_ids_eval": len({r.get("request_id") for r in requests}),
            "request_ids_with_samples": len(request_ids),
            "payment_option_ids": len(option_ids),
        }

    def _check_images(self) -> None:
        missing = []
        for row in self.rows.get("images.csv", []):
            image_id = row.get("image_id", "")
            expected = self.dataset_dir / "media" / "images" / f"{image_id}.png"
            if not expected.is_file():
                missing.append({"image_id": image_id, "path": str(expected)})
        if missing:
            self.issues["missing_image_files"].extend(missing)
        self.observed["image_files"] = {
            "mapped": len(self.rows.get("images.csv", [])),
            "missing": len(missing),
        }

    def _check_profiles(self) -> None:
        delimiter_observations: dict[str, list[str]] = {}
        for field in LIST_FIELDS:
            values = [r.get(field, "") for r in self.rows.get("financial_profiles.csv", [])]
            delimiters = sorted({d for value in values for d in ("|", ";", ",") if d in value})
            delimiter_observations[field] = delimiters
            if any(";" in value or "," in value for value in values):
                self.issue("unexpected_list_delimiters", {"field": field, "values": delimiters})
        self.observed["list_field_delimiters"] = delimiter_observations

        for row_number, row in enumerate(self.rows.get("financial_profiles.csv", []), start=2):
            try:
                balance = _parse_decimal(row.get("current_available_balance", ""))
                minimum = _parse_decimal(row.get("minimum_balance_to_keep", ""))
                if balance is not None and minimum is not None and minimum > balance:
                    self.issue("profile_invariants", {
                        "row": row_number, "user_id": row.get("user_id"),
                        "invariant": "minimum_balance_to_keep<=current_available_balance",
                    })
            except InvalidOperation:
                pass
            methods = set(filter(None, row.get("payment_methods_user_will_consider", "").split("|")))
            if not methods.issubset({"full_payment", "partial_payment", "installments"}):
                self.issue("unsupported_profile_payment_methods", {
                    "row": row_number, "user_id": row.get("user_id"), "values": sorted(methods),
                })
            months = row.get("max_installment_months", "")
            if not _blank(months):
                try:
                    if int(months) <= 0:
                        raise ValueError
                except ValueError:
                    self.issue("profile_invariants", {
                        "row": row_number, "user_id": row.get("user_id"),
                        "invariant": "max_installment_months is a positive integer or blank",
                        "value": months,
                    })

    def _check_events(self) -> None:
        events = self.rows.get("financial_events.csv", [])
        blank_amounts = [
            {"event_id": r.get("event_id"), "user_id": r.get("user_id"),
             "event_date": r.get("event_date"), "description": r.get("description")}
            for r in events if _blank(r.get("amount"))
        ]
        self.observed["blank_amount_events"] = blank_amounts
        if blank_amounts:
            self.issues["blank_amount_events"].extend(blank_amounts)
        for row_number, row in enumerate(events, start=2):
            if row.get("settlement_date", "") == "" and row.get("status") != "unrealized":
                self.issue("event_invariants", {
                    "row": row_number, "event_id": row.get("event_id"),
                    "invariant": "blank settlement_date only for unrealized records",
                })
            if row.get("flexibility") in {"reducible", "reducible_or_stoppable"}:
                if _blank(row.get("minimum_allowed_amount")):
                    self.issue("event_invariants", {
                        "row": row_number, "event_id": row.get("event_id"),
                        "invariant": "reducible event has minimum_allowed_amount",
                    })
            if row.get("settlement_date") and row.get("event_date"):
                try:
                    if _parse_date(row["settlement_date"]) < _parse_date(row["event_date"]):
                        self.issue("event_invariants", {
                            "row": row_number, "event_id": row.get("event_id"),
                            "invariant": "settlement_date>=event_date",
                        })
                except ValueError:
                    pass

    def _check_rates(self) -> None:
        profiles = {r.get("user_id"): r for r in self.rows.get("financial_profiles.csv", [])}
        rate_keys = {
            (r.get("rate_date"), r.get("from_currency"), r.get("to_currency"))
            for r in self.rows.get("exchange_rates.csv", [])
        }
        missing = []
        for row in self.rows.get("financial_events.csv", []):
            profile = profiles.get(row.get("user_id"))
            settlement = row.get("settlement_date", "")
            if not profile or _blank(settlement) or row.get("currency") == profile.get("home_currency"):
                continue
            key = (settlement, row.get("currency"), profile.get("home_currency"))
            if key not in rate_keys:
                missing.append({
                    "event_id": row.get("event_id"), "rate_date": settlement,
                    "from_currency": row.get("currency"), "to_currency": profile.get("home_currency"),
                })
        self.observed["fx_coverage"] = {
            "directed_rate_keys": len(rate_keys),
            "foreign_events_checked": sum(
                1 for r in self.rows.get("financial_events.csv", [])
                if profiles.get(r.get("user_id"), {}).get("home_currency") != r.get("currency")
                and not _blank(r.get("settlement_date"))
            ),
            "missing_required_rates": len(missing),
        }
        if missing:
            self.issues["missing_exchange_rates"].extend(missing)

    def _check_precision(self) -> None:
        profiles = {
            row.get("user_id"): row
            for row in self.rows.get("financial_profiles.csv", [])
        }
        requests = {
            row.get("request_id"): row
            for row in self.rows.get("requests.csv", []) + self.rows.get("sample_requests.csv", [])
        }
        scales: dict[str, Counter[int]] = defaultdict(Counter)

        def add(currency: str | None, value: str | None) -> None:
            precision = _scale(value or "")
            if currency and precision is not None:
                scales[currency][precision] += 1

        for row in self.rows.get("financial_events.csv", []):
            profile = profiles.get(row.get("user_id"), {})
            add(row.get("currency"), row.get("amount"))
            add(row.get("currency"), row.get("minimum_allowed_amount"))
            # This also makes it visible if a later implementation assumes
            # profile-home precision for every historical event.
            if profile.get("home_currency") != row.get("currency"):
                self.observed.setdefault("foreign_event_currencies", set()).add(row.get("currency"))
        for row in self.rows.get("financial_profiles.csv", []):
            add(row.get("home_currency"), row.get("current_available_balance"))
            add(row.get("home_currency"), row.get("minimum_balance_to_keep"))
        for row in self.rows.get("requests.csv", []) + self.rows.get("sample_requests.csv", []):
            add(profiles.get(row.get("user_id"), {}).get("home_currency"), row.get("requested_amount"))
        for row in self.rows.get("request_payment_options.csv", []):
            request = requests.get(row.get("request_id"), {})
            add(profiles.get(request.get("user_id"), {}).get("home_currency"), row.get("payment_amount"))
            add(profiles.get(request.get("user_id"), {}).get("home_currency"), row.get("financing_fee"))
            add(profiles.get(request.get("user_id"), {}).get("home_currency"), row.get("total_payable_amount"))
        self.observed["currency_precision"] = {
            currency: {str(scale): count for scale, count in sorted(counter.items())}
            for currency, counter in sorted(scales.items())
        }
        foreign = self.observed.get("foreign_event_currencies")
        if isinstance(foreign, set):
            self.observed["foreign_event_currencies"] = sorted(foreign)

    def _check_payment_options(self) -> None:
        options = self.rows.get("request_payment_options.csv", [])
        requests = {
            row.get("request_id"): row
            for row in self.rows.get("requests.csv", []) + self.rows.get("sample_requests.csv", [])
        }
        counts = Counter(r.get("request_id") for r in options)
        bad_counts = {request_id: count for request_id, count in counts.items() if count < 2 or count > 4}
        if bad_counts:
            self.issue("payment_option_count_errors", bad_counts)
        schedule_errors = []
        arithmetic_errors = []
        fee_errors = []
        for row_number, row in enumerate(options, start=2):
            method = row.get("payment_method")
            try:
                count = int(row.get("number_of_payments", ""))
            except ValueError:
                schedule_errors.append({"row": row_number, "reason": "invalid number_of_payments"})
                continue
            frequency = row.get("payment_frequency_days", "")
            if method == "full_payment" and (count != 1 or not _blank(frequency)):
                schedule_errors.append({"row": row_number, "reason": "full_payment must be one payment with blank frequency"})
            if method == "installments":
                if count <= 1 or _blank(frequency):
                    schedule_errors.append({"row": row_number, "reason": "installments need count>1 and frequency"})
                else:
                    try:
                        if int(frequency) <= 0:
                            raise ValueError
                    except ValueError:
                        schedule_errors.append({"row": row_number, "reason": "frequency must be positive integer"})
            try:
                payment = _parse_decimal(row.get("payment_amount", ""))
                fee = _parse_decimal(row.get("financing_fee", ""))
                total = _parse_decimal(row.get("total_payable_amount", ""))
                if payment is not None and fee is not None and total is not None:
                    # The supplied installment amount already represents each
                    # scheduled payment.  `financing_fee` is separately
                    # reconciled to requested_amount; it is not added a second
                    # time to total_payable_amount.
                    if payment * count != total:
                        arithmetic_errors.append({
                            "row": row_number, "payment_option_id": row.get("payment_option_id"),
                            "expected_total": str(payment * count), "actual_total": str(total),
                        })
                    request = requests.get(row.get("request_id"))
                    if request is not None:
                        requested = _parse_decimal(request.get("requested_amount", ""))
                        if requested is not None and total - requested != fee:
                            fee_errors.append({
                                "row": row_number, "payment_option_id": row.get("payment_option_id"),
                                "expected_fee": str(total - requested), "actual_fee": str(fee),
                            })
            except (InvalidOperation, TypeError):
                pass
        if schedule_errors:
            self.issues["payment_schedule_errors"].extend(schedule_errors)
        if arithmetic_errors:
            self.issues["payment_arithmetic_errors"].extend(arithmetic_errors)
        if fee_errors:
            self.issues["payment_fee_errors"].extend(fee_errors)
        self.observed["payment_options"] = {
            "option_count_by_request": dict(sorted(counts.items())),
            "requests_with_2_to_4_options": sum(2 <= count <= 4 for count in counts.values()),
            "schedule_error_count": len(schedule_errors),
            "arithmetic_error_count": len(arithmetic_errors),
            "fee_error_count": len(fee_errors),
            "payment_methods": sorted(set(r.get("payment_method") for r in options)),
            "frequency_days": sorted(set(r.get("payment_frequency_days") or "<BLANK>" for r in options)),
        }

    def _check_samples(self) -> None:
        violations = []
        for row_number, row in enumerate(self.rows.get("sample_requests.csv", []), start=2):
            method = row.get("recommended_payment_method")
            plan = row.get("payment_plan")
            if method == "wait" and plan == "none":
                violations.append({"row": row_number, "request_id": row.get("request_id"), "rule": "wait_plan_is_future_payment"})
            if method == "not_recommended" and plan != "none":
                violations.append({"row": row_number, "request_id": row.get("request_id"), "rule": "not_recommended_plan_is_none"})
        if violations:
            self.issue("sample_convention_violations", violations)
        self.observed["sample_conventions"] = {
            "wait_uses_future_plan": not any(v["rule"] == "wait_plan_is_future_payment" for v in violations),
            "not_recommended_uses_none": not any(v["rule"] == "not_recommended_plan_is_none" for v in violations),
        }

    def run(self) -> dict[str, object]:
        self.load()
        self._check_dates("requests.csv", {"request_date": False, "desired_completion_date": False})
        self._check_dates("sample_requests.csv", {"request_date": False, "desired_completion_date": False, "earliest_date_for_full_payment": True})
        self._check_dates("financial_events.csv", {"event_date": False, "settlement_date": True})
        self._check_dates("exchange_rates.csv", {"rate_date": False})
        self._check_dates("request_payment_options.csv", {"first_payment_date": False})
        self._check_dates("messages.csv", {"sent_at": False}, datetime_fields={"sent_at"})
        self._check_decimal_fields("requests.csv", {"requested_amount": False})
        self._check_decimal_fields("financial_profiles.csv", {"current_available_balance": False, "minimum_balance_to_keep": False})
        self._check_decimal_fields("financial_events.csv", {"amount": True, "minimum_allowed_amount": True})
        self._check_decimal_fields("exchange_rates.csv", {"rate": False})
        self._check_decimal_fields("request_payment_options.csv", {"payment_amount": False, "financing_fee": False, "total_payable_amount": False})
        self._check_enums("requests.csv", "request_type", REQUEST_TYPES)
        self._check_enums("requests.csv", "allows_partial_payment", {"true", "false"})
        self._check_enums("financial_events.csv", "event_type", EVENT_TYPES)
        self._check_enums("financial_events.csv", "direction", DIRECTIONS)
        self._check_enums("financial_events.csv", "status", STATUSES)
        self._check_enums("financial_events.csv", "flexibility", FLEXIBILITIES)
        self._check_enums("financial_events.csv", "currency", SUPPORTED_CURRENCIES)
        self._check_enums("exchange_rates.csv", "from_currency", SUPPORTED_CURRENCIES)
        self._check_enums("exchange_rates.csv", "to_currency", SUPPORTED_CURRENCIES)
        self._check_enums("request_payment_options.csv", "payment_method", {"full_payment", "installments"})
        self._check_enums("messages.csv", "source_type", SOURCE_TYPES)
        self._check_profiles()
        self._check_events()
        self._check_foreign_keys()
        self._check_images()
        self._check_rates()
        self._check_precision()
        self._check_payment_options()
        self._check_samples()

        issues = {key: value for key, value in sorted(self.issues.items()) if value}
        error_categories = {
            "missing_files", "unreadable_files", "schema_errors", "malformed_dates",
            "malformed_amounts", "negative_amounts", "invalid_foreign_keys", "cross_user_links",
            "duplicate_ids", "unsupported_enums", "missing_image_files", "missing_exchange_rates",
            "payment_schedule_errors", "payment_arithmetic_errors", "payment_option_count_errors",
            "payment_fee_errors",
        }
        error_count = sum(len(values) for key, values in issues.items() if key in error_categories)
        warning_count = sum(len(values) for key, values in issues.items() if key not in error_categories)
        return {
            "audit_version": "phase-0.v1",
            "generated_at_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "dataset_dir": str(self.dataset_dir),
            "assumptions": {
                key: value for key, value in ASSUMPTIONS.__dict__.items()
            },
            "files": self.file_info,
            "observed": self.observed,
            "issues": issues,
            "summary": {
                "ok": error_count == 0,
                "error_count": error_count,
                "warning_count": warning_count,
                "error_categories": sorted(key for key in issues if key in error_categories),
                "warning_categories": sorted(key for key in issues if key not in error_categories),
            },
        }


def audit_dataset(dataset_dir: Path = DATASET_DIR) -> dict[str, object]:
    """Run the audit and return a JSON-serializable report."""
    return DatasetAudit(Path(dataset_dir)).run()


def write_report(report: dict[str, object], output_path: Path = AUDIT_OUTPUT) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the Buy or Wait? input dataset")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--output", type=Path, default=AUDIT_OUTPUT)
    parser.add_argument("--strict", action="store_true", help="exit 1 when structural errors are found")
    args = parser.parse_args(argv)
    report = audit_dataset(args.dataset)
    write_report(report, args.output)
    summary = report["summary"]
    print(json.dumps({"output": str(args.output.resolve()), **summary}, sort_keys=True))
    return 1 if args.strict and not summary["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
