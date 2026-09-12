"""Phase 2 canonical ledger, lifecycle resolution, and cash-state matrix.

This module is intentionally deterministic.  Evidence can amend a supplied
row, but it cannot invent a payment decision.  Every later forecast should use
the ``CashEffect`` records emitted here rather than reinterpreting raw CSV
statuses.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .config import DATASET_DIR
from .fx import RateBook
from .loaders import Dataset, load_dataset, parse_date
from .models import (
    CanonicalLedger,
    CashEffect,
    EvidenceClaim,
    Event,
    Lifecycle,
    ResolvedEvent,
)
from .money import decimal_text, parse_decimal


class LedgerError(ValueError):
    """A structural or financial invariant prevents a safe ledger build."""


_EXCLUDED_STATUSES = frozenset({"failed", "cancelled", "unrealized"})
_INCLUDED_STATUS_RANK = {"pending": 2, "scheduled": 3, "settled": 4}


def _claim_from_fact(
    fact: dict[str, Any], *, related_event_id: str | None = None
) -> EvidenceClaim | None:
    source_id = str(fact.get("source_id", ""))
    claim_type = fact.get("claim_type")
    if not source_id or not claim_type or claim_type == "ignore":
        return None
    amount = parse_decimal(fact.get("amount"), allow_blank=True, field="evidence.amount")
    effective_date = None
    if fact.get("effective_date") is not None:
        effective_date = parse_date(str(fact["effective_date"]))
    confidence = Decimal(str(fact.get("confidence", 0)))
    return EvidenceClaim(
        source_id=source_id,
        claim_type=str(claim_type),
        target_event_id=fact.get("target_event_id") or related_event_id,
        amount=amount,
        currency=(str(fact["currency"]).upper() if fact.get("currency") else None),
        effective_date=effective_date,
        status=fact.get("status"),
        confidence=confidence,
        related_event_id=related_event_id,
    )


def evidence_claims(report: dict[str, Any] | None) -> list[EvidenceClaim]:
    """Normalise a Phase 1 report without trusting its free-form text fields."""
    if not report:
        return []
    claims: list[EvidenceClaim] = []
    for record in report.get("records", []):
        if not isinstance(record, dict):
            continue
        for fact in record.get("message_facts", []):
            if isinstance(fact, dict):
                claim = _claim_from_fact(fact)
                if claim:
                    claims.append(claim)
        for fact in record.get("image_facts", []):
            if not isinstance(fact, dict) or fact.get("status") == "unresolved":
                continue
            claim = _claim_from_fact(
                {
                    **fact,
                    "claim_type": "amend" if fact.get("amount") is not None else "status",
                    "target_event_id": fact.get("related_event_id"),
                    "effective_date": fact.get("date"),
                },
                related_event_id=fact.get("related_event_id"),
            )
            if claim:
                claims.append(claim)
    return claims


def _linked_components(events: dict[str, Event]) -> tuple[dict[str, Lifecycle], list[dict[str, Any]]]:
    """Build undirected transitive lifecycle components from directed links."""
    parent: dict[str, str] = {event_id: event_id for event_id in events}
    diagnostics: list[dict[str, Any]] = []

    def find(event_id: str) -> str:
        while parent[event_id] != event_id:
            parent[event_id] = parent[parent[event_id]]
            event_id = parent[event_id]
        return event_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for event in events.values():
        if not event.linked_event_id:
            continue
        linked = events.get(event.linked_event_id)
        if linked is None:
            diagnostics.append({"event_id": event.event_id, "linked_event_id": event.linked_event_id, "reason": "unknown_link"})
            continue
        if linked.user_id != event.user_id:
            diagnostics.append({"event_id": event.event_id, "linked_event_id": event.linked_event_id, "reason": "cross_user_link"})
            continue
        union(event.event_id, linked.event_id)

    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for event_id in events:
        grouped[find(event_id)].append(event_id)
    lifecycles: dict[str, Lifecycle] = {}
    for event_ids in grouped.values():
        ordered = tuple(sorted(event_ids))
        lifecycle_id = f"lifecycle:{ordered[0]}"
        user_id = events[ordered[0]].user_id
        lifecycles[lifecycle_id] = Lifecycle(lifecycle_id, user_id, ordered)
    return lifecycles, diagnostics


def _lifecycle_index(lifecycles: dict[str, Lifecycle]) -> dict[str, str]:
    return {
        event_id: lifecycle_id
        for lifecycle_id, lifecycle in lifecycles.items()
        for event_id in lifecycle.event_ids
    }


def _resolve_event(event: Event, claims: Iterable[EvidenceClaim]) -> ResolvedEvent:
    amount = event.amount
    currency = event.currency
    effective_date = event.settlement_date or event.event_date
    status = event.status
    confirmed = False
    evidence_ids: list[str] = []
    applied: list[str] = []

    # Specific state-changing claims are applied before weaker descriptive
    # claims.  Source IDs make ties deterministic and auditable.
    priority = {"cancel": 50, "amend": 45, "status": 40, "delay": 35, "confirm": 30, "date": 20, "amount": 15, "recurrence": 5}
    ordered_claims = sorted(claims, key=lambda c: (priority.get(c.claim_type, 0), c.source_id))
    for claim in ordered_claims:
        evidence_ids.append(claim.source_id)
        applied.append(f"{claim.source_id}:{claim.claim_type}")
        if claim.claim_type == "cancel":
            status = "cancelled"
        elif claim.claim_type == "delay":
            status = "pending"
        elif claim.claim_type == "status" and claim.status in {"pending", "failed", "cancelled", "scheduled", "settled", "unrealized"}:
            status = str(claim.status)
        elif claim.claim_type == "confirm":
            confirmed = True
            if claim.status == "settled":
                status = "settled"
        if claim.amount is not None and claim.claim_type in {"amount", "amend", "status", "confirm"}:
            amount = claim.amount
        if claim.currency and claim.claim_type in {"amount", "amend"}:
            currency = claim.currency
        if claim.effective_date and claim.claim_type in {"date", "delay", "amend", "status", "confirm"}:
            effective_date = claim.effective_date
        if claim.status == "confirmed":
            confirmed = True

    unresolved_reason = "unresolved_amount" if amount is None else None
    return ResolvedEvent(
        event=event,
        amount=amount,
        currency=currency,
        effective_date=effective_date,
        status=status,
        confirmed=confirmed,
        source_row_ids=(event.event_id,),
        evidence_source_ids=tuple(sorted(set(evidence_ids))),
        unresolved_reason=unresolved_reason,
        applied_claims=tuple(applied),
    )


def cash_flow_effect(
    resolved: ResolvedEvent,
    *,
    lifecycle_id: str,
    home_currency: str,
    rates: RateBook,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CashEffect:
    """Map one resolved row through the complete challenge cash-state matrix."""
    event = resolved.event
    base = dict(
        event_id=event.event_id,
        lifecycle_id=lifecycle_id,
        user_id=event.user_id,
        direction=event.direction,
        home_currency=home_currency,
        effective_date=resolved.effective_date,
        status=resolved.status,
        category=event.category,
        event_type=event.event_type,
        flexibility=event.flexibility,
        minimum_allowed_amount=event.minimum_allowed_amount,
        source_row_ids=resolved.source_row_ids,
        evidence_source_ids=resolved.evidence_source_ids,
    )
    if resolved.amount is None:
        return CashEffect(amount=None, included=False, reserve=False, reason="unresolved_amount", **base)
    if event.direction == "non_cash":
        return CashEffect(amount=None, included=False, reserve=False, reason="non_cash", **base)
    if resolved.status in _EXCLUDED_STATUSES:
        return CashEffect(amount=None, included=False, reserve=False, reason=f"status_{resolved.status}", **base)
    if resolved.effective_date is None:
        return CashEffect(amount=None, included=False, reserve=False, reason="missing_effective_date", **base)
    if start_date and resolved.effective_date < start_date or end_date and resolved.effective_date > end_date:
        return CashEffect(amount=None, included=False, reserve=False, reason="outside_window", **base)
    if resolved.status == "pending" and event.direction == "credit":
        return CashEffect(amount=None, included=False, reserve=False, reason="pending_credit_excluded", **base)
    if resolved.status == "scheduled" and event.direction == "credit" and not resolved.confirmed:
        return CashEffect(amount=None, included=False, reserve=False, reason="unconfirmed_scheduled_credit", **base)
    if resolved.status not in {"settled", "pending", "scheduled"}:
        return CashEffect(amount=None, included=False, reserve=False, reason=f"unsupported_status_{resolved.status}", **base)

    converted = rates.convert_to_home_currency(
        resolved.amount, resolved.currency, home_currency, resolved.effective_date
    )
    reserve = resolved.status == "pending" and event.direction == "debit"
    return CashEffect(
        amount=converted,
        included=True,
        reserve=reserve,
        reason="pending_debit_reserved" if reserve else f"{resolved.status}_{event.direction}_included",
        **base,
    )


def _deduplicate_effects(
    effects: list[CashEffect], lifecycles: dict[str, Lifecycle]
) -> tuple[list[CashEffect], list[dict[str, Any]]]:
    """Deduplicate same-kind linked cash effects while retaining audit rows."""
    by_lifecycle: defaultdict[str, list[CashEffect]] = defaultdict(list)
    for effect in effects:
        if effect.included:
            by_lifecycle[effect.lifecycle_id].append(effect)
    duplicates: list[dict[str, Any]] = []
    replacements: dict[str, tuple[str, str]] = {}
    for lifecycle_id, rows in by_lifecycle.items():
        groups: defaultdict[tuple[str, str, str], list[CashEffect]] = defaultdict(list)
        for effect in rows:
            groups[(effect.direction, effect.event_type, effect.category)].append(effect)
        for key, group in groups.items():
            if len(group) < 2:
                continue
            # A linked same-kind record is one cash effect.  Prefer explicit
            # settled state, then scheduled, then pending, and finally the
            # newer effective date.  Opposing directions/types remain separate.
            winner = max(
                group,
                key=lambda effect: (
                    _INCLUDED_STATUS_RANK.get(effect.status, 0),
                    effect.effective_date or date.min,
                    effect.event_id,
                ),
            )
            for loser in group:
                if loser.event_id == winner.event_id:
                    continue
                replacements[loser.event_id] = (winner.event_id, "duplicate_linked_cash_effect")
                duplicates.append({
                    "lifecycle_id": lifecycle_id,
                    "event_id": loser.event_id,
                    "kept_event_id": winner.event_id,
                    "group": list(key),
                })

    result: list[CashEffect] = []
    for effect in effects:
        replacement = replacements.get(effect.event_id)
        if replacement:
            winner_id, reason = replacement
            result.append(CashEffect(**{
                **asdict(effect),
                "amount": None,
                "included": False,
                "reserve": False,
                "reason": f"{reason}:{winner_id}",
            }))
        else:
            result.append(effect)
    return result, duplicates


def build_ledger(dataset: Dataset, report: dict[str, Any] | None = None) -> CanonicalLedger:
    lifecycles, link_diagnostics = _linked_components(dataset.events)
    event_to_lifecycle = _lifecycle_index(lifecycles)
    claims_by_event: defaultdict[str, list[EvidenceClaim]] = defaultdict(list)
    unscoped_claims: list[str] = []
    claims = evidence_claims(report)
    for claim in claims:
        if claim.target_event_id and claim.target_event_id in dataset.events:
            claims_by_event[claim.target_event_id].append(claim)
        else:
            unscoped_claims.append(claim.source_id)

    resolved: dict[str, ResolvedEvent] = {}
    effects: list[CashEffect] = []
    for event_id in sorted(dataset.events):
        event = dataset.events[event_id]
        row = _resolve_event(event, claims_by_event.get(event_id, ()))
        resolved[event_id] = row
        effects.append(cash_flow_effect(
            row,
            lifecycle_id=event_to_lifecycle[event_id],
            home_currency=dataset.profiles[event.user_id].home_currency,
            rates=dataset.rates,
        ))
    effects, duplicates = _deduplicate_effects(effects, lifecycles)
    diagnostics = {
        "lifecycle_count": len(lifecycles),
        "event_count": len(dataset.events),
        "evidence_claim_count": len(claims),
        "evidence_claims_applied": sum(bool(row.applied_claims) for row in resolved.values()),
        "unscoped_evidence_claim_count": len(unscoped_claims),
        "unscoped_evidence_sources": sorted(set(unscoped_claims)),
        "link_diagnostics": link_diagnostics,
        "duplicate_cash_effects": duplicates,
        "unresolved_amount_event_ids": sorted(
            event_id for event_id, row in resolved.items() if row.unresolved_reason
        ),
        "effect_counts": {
            "included": sum(effect.included and not effect.reserve for effect in effects),
            "reserved_pending_debits": sum(effect.included and effect.reserve for effect in effects),
            "excluded": sum(not effect.included for effect in effects),
        },
    }
    return CanonicalLedger(
        profiles=dataset.profiles,
        events=dataset.events,
        lifecycles=lifecycles,
        resolved_events=resolved,
        effects=effects,
        diagnostics=diagnostics,
    )


def build_ledger_from_directory(
    dataset_dir: Path = DATASET_DIR, evidence_report_path: Path | None = None
) -> CanonicalLedger:
    report = None
    if evidence_report_path and evidence_report_path.is_file():
        report = json.loads(evidence_report_path.read_text(encoding="utf-8"))
    return build_ledger(load_dataset(dataset_dir), report)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def ledger_report(ledger: CanonicalLedger) -> dict[str, Any]:
    return {
        "ledger_version": "phase-2.v1",
        "balance_anchor": "profile_as_of_request",
        "cash_flow_date": "settlement_date_when_present_else_event_date",
        "fx_policy": "exact_directed_rate_on_effective_settlement_date",
        "diagnostics": _json_value(ledger.diagnostics),
        "lifecycles": [
            _json_value(asdict(lifecycle))
            for lifecycle in sorted(ledger.lifecycles.values(), key=lambda row: row.lifecycle_id)
        ],
        "effects": [_json_value(asdict(effect)) for effect in ledger.effects],
    }


def write_ledger_report(ledger: CanonicalLedger, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(ledger_report(ledger), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the canonical Phase 2 ledger")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    ledger = build_ledger_from_directory(args.dataset, args.evidence)
    if args.output:
        write_ledger_report(ledger, args.output)
    print(json.dumps({
        "output": str(args.output.resolve()) if args.output else None,
        "event_count": ledger.diagnostics["event_count"],
        "lifecycle_count": ledger.diagnostics["lifecycle_count"],
        "evidence_claim_count": ledger.diagnostics["evidence_claim_count"],
        "evidence_claims_applied": ledger.diagnostics["evidence_claims_applied"],
        "effect_counts": ledger.diagnostics["effect_counts"],
        "unresolved_amount_count": len(ledger.diagnostics["unresolved_amount_event_ids"]),
        "duplicate_cash_effect_count": len(ledger.diagnostics["duplicate_cash_effects"]),
        "link_diagnostic_count": len(ledger.diagnostics["link_diagnostics"]),
    }, sort_keys=True, default=_json_value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
