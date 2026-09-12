"""Run the production planner against the public 25-row calibration set."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))

from buy_wait.candidates import load_payment_options, plan_request  # noqa: E402
from buy_wait.config import DATASET_DIR  # noqa: E402
from buy_wait.event_resolution import build_ledger  # noqa: E402
from buy_wait.evidence import EvidencePipeline  # noqa: E402
from buy_wait.explanations import build_decision_trace, output_row  # noqa: E402
from buy_wait.loaders import load_dataset, read_csv  # noqa: E402
from buy_wait.zenmux import DEFAULT_MODEL  # noqa: E402


FIELDS = (
    "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
)


def _normal(value: Any, field: str = "") -> str:
    if value is None:
        return ""
    if field == "amount_safe_to_pay":
        try:
            return format(Decimal(str(value)), "f")
        except Exception:
            pass
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def score_samples(
    dataset_dir: Path = DATASET_DIR,
    samples_path: Path | None = None,
    cache_path: Path | None = None,
    *,
    horizon_days: int = 90,
) -> dict[str, Any]:
    root = Path(dataset_dir).resolve()
    samples = Path(samples_path or root / "sample_requests.csv")
    requests = read_csv(samples)
    # Prefer the model-qualified cache when it exists; no API client is
    # created here, so scoring remains zero-call and deterministic.
    evidence = EvidencePipeline(root, cache_path, cache_only=True, extractor_model=DEFAULT_MODEL).run(requests)
    ledger = build_ledger(load_dataset(root), evidence)
    options = load_payment_options(root)
    predicted: list[dict[str, str]] = []
    debug: list[dict[str, Any]] = []
    for request in requests:
        plan = plan_request(
            ledger, request, options=options.get(request.get("request_id", ""), ()),
            evidence_report=evidence, horizon_days=horizon_days,
        )
        row = output_row(plan, build_decision_trace(plan, ledger, evidence_report=evidence), ledger)
        predicted.append(row)
        debug.append({
            "request_id": plan.request_id,
            "selected_method": plan.selected.method,
            "selected_status": plan.selected.status,
            "candidate_count": len(plan.candidates),
            "candidate_methods": [candidate.method for candidate in plan.candidates],
            "selected_rationale": plan.selected.rationale,
        })
    field_matches = {field: 0 for field in FIELDS}
    diffs: list[dict[str, Any]] = []
    for expected, actual, details in zip(requests, predicted, debug):
        expected_fields = {field: expected.get(field, "") for field in FIELDS}
        row_diffs = {
            field: {"expected": expected_fields[field], "actual": actual.get(field, "")}
            for field in FIELDS
            if _normal(expected_fields[field], field) != _normal(actual.get(field, ""), field)
        }
        for field in FIELDS:
            if field not in row_diffs:
                field_matches[field] += 1
        if row_diffs:
            diffs.append({"request_id": expected.get("request_id", ""), "fields": row_diffs, "debug": details})
    count = len(requests)
    report = {
        "score_version": "phase-7.v1",
        "dataset_dir": str(root),
        "sample_path": str(samples.resolve()),
        "request_count": count,
        "exact_output_row_matches": count - len(diffs),
        "exact_output_row_match_rate": (count - len(diffs)) / count if count else 1.0,
        "field_matches": field_matches,
        "field_match_rates": {field: (value / count if count else 1.0) for field, value in field_matches.items()},
        "mismatch_count": len(diffs),
        "mismatches": diffs,
        "evidence_summary": evidence.get("summary", {}),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the production solver on public samples")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR)
    parser.add_argument("--samples", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizon-days", type=int, default=90)
    args = parser.parse_args(argv)
    report = score_samples(args.dataset, args.samples, args.cache, horizon_days=args.horizon_days)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "request_count": report["request_count"],
        "exact_output_row_matches": report["exact_output_row_matches"],
        "field_match_rates": report["field_match_rates"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
