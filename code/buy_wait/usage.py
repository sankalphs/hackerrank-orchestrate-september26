"""Small, secret-free model usage accounting for the submission artifact."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    model: str
    purpose: str
    source_id: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int
    estimated_cost: Decimal = Decimal("0")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageTracker:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, value: UsageRecord) -> None:
        if value.input_tokens < 0 or value.output_tokens < 0 or value.estimated_cost < 0:
            raise ValueError("usage values must be non-negative")
        self.records.append(value)

    def totals(self) -> tuple[int, int, int, Decimal]:
        return (
            sum(row.input_tokens for row in self.records),
            sum(row.output_tokens for row in self.records),
            sum(row.total_tokens for row in self.records),
            sum((row.estimated_cost for row in self.records), Decimal("0")),
        )

    @property
    def model_call_count(self) -> int:
        return sum(1 for row in self.records if not row.cache_hit)

    @property
    def cache_hit_count(self) -> int:
        return sum(1 for row in self.records if row.cache_hit)


def write_usage_report(path: Path, *, request_count: int, tracker: UsageTracker | None = None, mode: str = "offline") -> None:
    tracker = tracker or UsageTracker()
    input_tokens, output_tokens, total_tokens, cost = tracker.totals()
    average_tokens = Decimal(total_tokens) / request_count if request_count else Decimal("0")
    average_cost = cost / request_count if request_count else Decimal("0")
    by_model: dict[tuple[str, str], list[UsageRecord]] = {}
    for record in tracker.records:
        by_model.setdefault((record.provider, record.model), []).append(record)
    lines = [
        "# Model usage report",
        "",
        f"Mode: {mode}",
        f"Requests: {request_count}",
        f"Model calls: {tracker.model_call_count}",
        f"Cache hits: {tracker.cache_hit_count}",
        f"Input tokens: {input_tokens}",
        f"Output tokens: {output_tokens}",
        f"Total tokens: {total_tokens}",
        f"Average tokens per request: {average_tokens}",
        f"Estimated total cost: ${cost}",
        f"Estimated cost per request: ${average_cost}",
        "",
        "| Provider | Model | Calls | Input tokens | Output tokens | Total tokens | Estimated cost |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    if by_model:
        for (provider, model), records in sorted(by_model.items()):
            in_tokens = sum(row.input_tokens for row in records)
            out_tokens = sum(row.output_tokens for row in records)
            model_cost = sum((row.estimated_cost for row in records), Decimal("0"))
            calls = sum(1 for row in records if not row.cache_hit)
            lines.append(f"| {provider} | {model} | {calls} | {in_tokens} | {out_tokens} | {in_tokens + out_tokens} | ${model_cost} |")
    else:
        lines.append("| none | none | 0 | 0 | 0 | 0 | $0 |")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)


__all__ = ["UsageRecord", "UsageTracker", "write_usage_report"]
