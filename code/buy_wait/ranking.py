"""Pure deterministic ranking for Phase 5 payment candidates."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .candidates import PaymentCandidate

def candidate_sort_key(candidate: PaymentCandidate) -> tuple[object, ...]:
    """Return the exact lower-is-better ranking tuple from the spec.

    Method names are intentionally not a priority. The challenge ranks
    eligible plans by completion, changes, total paid, start date, payment
    count, and only then the supplied option/signature. A method shortcut
    could prefer a more expensive installment plan over a cheaper payment
    whenever both are safe.
    """
    return (
        not candidate.completes_by_deadline,
        bool(candidate.spending_changes),
        len(candidate.spending_changes),
        candidate.total_paid,
        candidate.first_payment_date,
        candidate.payment_count,
        candidate.payment_option_id or "",
        candidate.canonical_signature,
    )


def rank_candidates(candidates: list[PaymentCandidate] | tuple[PaymentCandidate, ...]) -> tuple[PaymentCandidate, ...]:
    return tuple(sorted(candidates, key=candidate_sort_key))


__all__ = ["candidate_sort_key", "rank_candidates"]
