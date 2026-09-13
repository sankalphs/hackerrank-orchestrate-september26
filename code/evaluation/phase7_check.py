"""Phase 7 release gates: structural, safety, calibration, and cache checks."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_COLUMNS = (
    "request_id", "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed", "decision_explanation",
)
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
PLAN_PART = re.compile(r"^\d{4}-\d{2}-\d{2}:[0-9]+(?:\.[0-9]+)?$")


def _read(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _gate(name: str, condition: bool, detail: str = "") -> dict[str, str | bool]:
    return {"name": name, "passed": bool(condition), "detail": detail}


def run_gates(root: Path = ROOT) -> dict[str, object]:
    output_path = root / "output.csv"
    requests = _read(root / "dataset" / "requests.csv")
    rows = _read(output_path) if output_path.is_file() else []
    gates: list[dict[str, str | bool]] = []
    gates.append(_gate("output_exists", output_path.is_file()))
    gates.append(_gate("exact_header", output_path.is_file() and tuple(rows[0].keys()) == OUTPUT_COLUMNS if rows else False))
    gates.append(_gate("row_count", len(rows) == len(requests), f"{len(rows)} vs {len(requests)}"))
    request_ids = [row.get("request_id", "") for row in rows]
    gates.append(_gate("unique_request_ids", len(request_ids) == len(set(request_ids))))
    gates.append(_gate("request_coverage", set(request_ids) == {row["request_id"] for row in requests}))
    request_by_id = {row["request_id"]: row for row in requests}
    bounds_ok = True
    for row in rows:
        try:
            safe = Decimal(row["amount_safe_to_pay"])
            requested = Decimal(request_by_id[row["request_id"]]["requested_amount"])
            bounds_ok &= Decimal("0") <= safe <= requested
        except (KeyError, ValueError):
            bounds_ok = False
    gates.append(_gate("safe_amount_bounds", bounds_ok))
    gates.append(_gate("status_enum", all(row.get("affordability_status") in STATUSES for row in rows)))
    gates.append(_gate("method_enum", all(row.get("recommended_payment_method") in METHODS for row in rows)))
    gates.append(_gate("not_recommended_none", all(row.get("recommended_payment_method") != "not_recommended" or row.get("payment_plan") == "none" for row in rows)))
    gates.append(_gate("now_has_request_date", all(row.get("affordability_status") != "affordable_now" or row.get("earliest_date_for_full_payment") == request_by_id[row["request_id"]]["request_date"] for row in rows)))
    plans_ok = all(
        row.get("payment_plan") == "none" or all(PLAN_PART.fullmatch(part) for part in row.get("payment_plan", "").split("|"))
        for row in rows
    )
    gates.append(_gate("plan_syntax", plans_ok))
    gates.append(_gate("explanations_present", all(row.get("decision_explanation", "").strip() for row in rows)))
    gates.append(_gate("explanations_grounded_length", all(len(row.get("decision_explanation", "").strip()) >= 20 for row in rows), "all explanations >=20 chars"))
    gates.append(_gate("validator_command", _run_command([sys.executable, "code/main.py", "validate", "output.csv"])))
    gates.append(_gate("audit_strict_command", _run_command([sys.executable, "code/main.py", "audit", "--strict"])))
    gates.append(_gate("unit_tests", _run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])))
    score_path = root / "code" / "evaluation" / "sample_score_report.json"
    score = json.loads(score_path.read_text(encoding="utf-8")) if score_path.is_file() else {}
    gates.append(_gate("sample_score_report", bool(score) and score.get("request_count") == 25))
    gates.append(_gate("sample_score_fields", bool(score) and set(score.get("field_matches", {})) >= {"affordability_status", "recommended_payment_method", "payment_plan"}))
    rates = score.get("field_match_rates", {}) if isinstance(score, dict) else {}
    tolerant = score.get("tolerant_rates", {}) if isinstance(score, dict) else {}
    gates.append(_gate("sample_status_accuracy", rates.get("affordability_status", 0) >= 0.70, f"{rates.get('affordability_status', 0):.2f} >= 0.70"))
    gates.append(_gate("sample_method_accuracy", rates.get("recommended_payment_method", 0) >= 0.70, f"{rates.get('recommended_payment_method', 0):.2f} >= 0.70"))
    gates.append(_gate("sample_plan_accuracy", rates.get("payment_plan", 0) >= 0.70, f"{rates.get('payment_plan', 0):.2f} >= 0.70"))
    gates.append(_gate("sample_amount_tolerant", tolerant.get("amount_safe_to_pay_tolerant", 0) >= 0.30, f"{tolerant.get('amount_safe_to_pay_tolerant', 0):.2f} >= 0.30"))
    gates.append(_gate("sample_explanation_useful", tolerant.get("decision_explanation_useful", 0) >= 0.70, f"{tolerant.get('decision_explanation_useful', 0):.2f} >= 0.70"))
    gates.append(_gate("sample_calibration_score", float(score.get("calibration_score", 0) or 0) >= 0.60, f"{float(score.get('calibration_score', 0) or 0):.3f} >= 0.60"))
    cache_path = root / "code" / "cache" / "evidence_cache.json"
    gates.append(_gate("cache_is_json", cache_path.is_file() and _valid_json(cache_path)))
    gates.append(_gate("cache_has_content_hashes", cache_path.is_file() and _cache_has_hashes(cache_path)))
    usage_path = root / "code" / "evaluation" / "usage_report.md"
    usage_text = usage_path.read_text(encoding="utf-8") if usage_path.is_file() else ""
    gates.append(_gate("usage_report_present", "Model calls:" in usage_text and "Average tokens per request:" in usage_text))
    gates.append(_gate("no_secret_in_usage_report", "ZENMUX_API_KEY" not in usage_text and "sk-" not in usage_text))
    passed = sum(1 for gate in gates if gate["passed"])
    return {"check_version": "phase-7.v2", "gate_count": len(gates), "passed": passed, "failed": len(gates) - passed, "gates": gates}


def _run_command(command: list[str]) -> bool:
    result = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def _valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, json.JSONDecodeError):
        return False


def _cache_has_hashes(path: Path) -> bool:
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
        return bool(entries) and all(len(str(key).split(":")) >= 4 for key in entries)
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Phase 7 release gates")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    report = run_gates()
    output = args.output or ROOT / "code" / "evaluation" / "phase7_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output.resolve()), "gate_count": report["gate_count"], "passed": report["passed"], "failed": report["failed"]}, sort_keys=True))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
