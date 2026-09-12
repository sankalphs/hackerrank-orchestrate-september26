"""Dependency-free typed loaders for the Phase 2 input boundary."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import DATASET_DIR
from .fx import RateBook
from .models import Event, ExchangeRate, Profile
from .money import parse_decimal


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_date(value: str, *, allow_blank: bool = False) -> date | None:
    if value is None or value.strip() == "":
        if allow_blank:
            return None
        raise ValueError("date is blank")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"invalid YYYY-MM-DD date: {value!r}") from exc


def split_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split("|") if item.strip())


def load_profiles(dataset_dir: Path = DATASET_DIR) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for row in read_csv(Path(dataset_dir) / "financial_profiles.csv"):
        months = row.get("max_installment_months", "").strip()
        profiles[row["user_id"]] = Profile(
            user_id=row["user_id"],
            home_currency=row["home_currency"],
            current_available_balance=parse_decimal(row["current_available_balance"], field="current_available_balance"),
            minimum_balance_to_keep=parse_decimal(row["minimum_balance_to_keep"], field="minimum_balance_to_keep"),
            financial_priorities=split_list(row.get("financial_priorities", "")),
            expense_categories_to_protect=split_list(row.get("expense_categories_to_protect", "")),
            expense_categories_user_is_willing_to_reduce=split_list(row.get("expense_categories_user_is_willing_to_reduce", "")),
            expense_categories_user_is_willing_to_stop=split_list(row.get("expense_categories_user_is_willing_to_stop", "")),
            payment_methods_user_will_consider=split_list(row.get("payment_methods_user_will_consider", "")),
            max_installment_months=int(months) if months else None,
        )
    return profiles


def load_events(dataset_dir: Path = DATASET_DIR) -> dict[str, Event]:
    events: dict[str, Event] = {}
    for row in read_csv(Path(dataset_dir) / "financial_events.csv"):
        events[row["event_id"]] = Event(
            event_id=row["event_id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            description=row["description"],
            category=row["category"],
            direction=row["direction"],
            amount=parse_decimal(row.get("amount"), allow_blank=True, field="event.amount"),
            currency=row["currency"],
            event_date=parse_date(row["event_date"]),
            settlement_date=parse_date(row.get("settlement_date", ""), allow_blank=True),
            status=row["status"],
            linked_event_id=row.get("linked_event_id") or None,
            flexibility=row["flexibility"],
            minimum_allowed_amount=parse_decimal(
                row.get("minimum_allowed_amount"), allow_blank=True, field="minimum_allowed_amount"
            ),
        )
    return events


def load_rates(dataset_dir: Path = DATASET_DIR) -> RateBook:
    rows = read_csv(Path(dataset_dir) / "exchange_rates.csv")
    return RateBook(
        ExchangeRate(
            rate_date=parse_date(row["rate_date"]),
            from_currency=row["from_currency"],
            to_currency=row["to_currency"],
            rate=parse_decimal(row["rate"], field="exchange rate"),
        )
        for row in rows
    )


@dataclass(frozen=True)
class Dataset:
    dataset_dir: Path
    profiles: dict[str, Profile]
    events: dict[str, Event]
    rates: RateBook


def load_dataset(dataset_dir: Path = DATASET_DIR) -> Dataset:
    root = Path(dataset_dir).resolve()
    return Dataset(root, load_profiles(root), load_events(root), load_rates(root))

