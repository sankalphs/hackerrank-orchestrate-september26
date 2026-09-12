"""Pure deterministic ranking for Phase 5 payment candidates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .candidates import PaymentCandidate


def candidate_sort_key(candidate: PaymentCandidate) -> tuple[object, ...]:
    """Return the exact lower-is-better ranking tuple from the plan."""
    return (
        not candidate.completes_by_deadline,
        bool(candidate.spending_changes),
        candidate.total_paid,
        candidate.first_payment_date,
        candidate.payment_count,
        candidate.payment_option_id or "",
        candidate.canonical_signature,
    )


def rank_candidates(candidates: list[PaymentCandidate] | tuple[PaymentCandidate, ...]) -> tuple[PaymentCandidate, ...]:
    return tuple(sorted(candidates, key=candidate_sort_key))


__all__ = ["candidate_sort_key", "rank_candidates"]
