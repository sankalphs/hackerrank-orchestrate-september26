"""Command-line entry point for the Buy or Wait? solution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Buy or Wait? financial agent")
    subparsers = parser.add_subparsers(dest="command")
    audit = subparsers.add_parser("audit", help="run the Phase 0 input audit")
    audit.add_argument("--dataset", type=Path, default=None)
    audit.add_argument("--output", type=Path, default=None)
    audit.add_argument("--strict", action="store_true")
    evidence = subparsers.add_parser("evidence", help="extract and validate untrusted evidence")
    evidence.add_argument("--dataset", type=Path, default=None)
    evidence.add_argument("--cache", type=Path, default=None)
    evidence.add_argument("--output", type=Path, default=None)
    evidence.add_argument("--request-id", default=None)
    evidence.add_argument("--user-id", default=None)
    evidence.add_argument("--cache-only", action="store_true")
    evidence.add_argument("--use-zenmux", action="store_true")
    evidence.add_argument("--max-model-calls", type=int, default=None)
    evidence.add_argument("--use-zenmux-messages", action="store_true")
    ledger = subparsers.add_parser("ledger", help="build the Phase 2 canonical ledger")
    ledger.add_argument("--dataset", type=Path, default=None)
    ledger.add_argument("--evidence", type=Path, default=None)
    ledger.add_argument("--output", type=Path, default=None)
    forecast = subparsers.add_parser("forecast", help="build the Phase 3 recurrence and forecast report")
    forecast.add_argument("--dataset", type=Path, default=None)
    forecast.add_argument("--evidence", type=Path, default=None)
    forecast.add_argument("--output", type=Path, default=None)
    forecast.add_argument("--request-id", default=None)
    forecast.add_argument("--user-id", default=None)
    forecast.add_argument("--horizon-days", type=int, default=90)
    capacity = subparsers.add_parser("capacity", help="build the Phase 4 baseline capacity report")
    capacity.add_argument("--dataset", type=Path, default=None)
    capacity.add_argument("--evidence", type=Path, default=None)
    capacity.add_argument("--output", type=Path, default=None)
    capacity.add_argument("--request-id", default=None)
    capacity.add_argument("--user-id", default=None)
    capacity.add_argument("--horizon-days", type=int, default=90)
    capacity.add_argument("--currency-unit", default=None)
    plan = subparsers.add_parser("plan", help="build the Phase 5 candidate and ranking report")
    plan.add_argument("--dataset", type=Path, default=None)
    plan.add_argument("--evidence", type=Path, default=None)
    plan.add_argument("--output", type=Path, default=None)
    plan.add_argument("--request-id", default=None)
    plan.add_argument("--user-id", default=None)
    plan.add_argument("--horizon-days", type=int, default=90)
    run = subparsers.add_parser("run", help="solve, explain, validate, and write output.csv")
    run.add_argument("--dataset", type=Path, default=None)
    run.add_argument("--output", type=Path, default=None)
    run.add_argument("--cache-dir", type=Path, default=None)
    run.add_argument("--offline", action="store_true", help="use cached evidence only")
    run.add_argument("--refresh-evidence", action="store_true")
    run.add_argument("--use-zenmux", action="store_true", help="use .env ZenMux credentials for uncached evidence only")
    run.add_argument("--max-model-calls", type=int, default=None)
    run.add_argument("--use-zenmux-messages", action="store_true")
    run.add_argument("--horizon-days", type=int, default=90)
    validate = subparsers.add_parser("validate", help="independently validate an output file")
    validate.add_argument("path", nargs="?", type=Path, default=None)
    validate.add_argument("--dataset", type=Path, default=None)
    validate.add_argument("--evidence", type=Path, default=None)
    validate.add_argument("--horizon-days", type=int, default=90)
    score = subparsers.add_parser("score-samples", help="calibrate against the public solved samples")
    score.add_argument("--dataset", type=Path, default=None)
    score.add_argument("--samples", type=Path, default=None)
    score.add_argument("--cache", type=Path, default=None)
    score.add_argument("--output", type=Path, default=None)
    score.add_argument("--horizon-days", type=int, default=90)
    check = subparsers.add_parser("check", help="run Phase 7 synthetic and property gates")
    check.add_argument("--output", type=Path, default=None)
    package = subparsers.add_parser("package", help="build the allow-listed code.zip submission")
    package.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command is None or args.command == "run":
        return _run_solution(args)
    if args.command == "audit":
        # Support both `python code/main.py audit` and module execution from root.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import AUDIT_OUTPUT, DATASET_DIR
        from buy_wait.input_audit import main as audit_main

        audit_args = []
        if args.dataset is not None:
            audit_args += ["--dataset", str(args.dataset)]
        else:
            audit_args += ["--dataset", str(DATASET_DIR)]
        if args.output is not None:
            audit_args += ["--output", str(args.output)]
        else:
            audit_args += ["--output", str(AUDIT_OUTPUT)]
        if args.strict:
            audit_args.append("--strict")
        return audit_main(audit_args)
    if args.command == "evidence":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import CACHE_DIR, DATASET_DIR, EVIDENCE_OUTPUT
        from buy_wait.evidence import main as evidence_main

        evidence_args = []
        evidence_args += ["--dataset", str(args.dataset or DATASET_DIR)]
        evidence_args += ["--cache", str(args.cache or CACHE_DIR / "evidence_cache.json")]
        evidence_args += ["--output", str(args.output or EVIDENCE_OUTPUT)]
        if args.request_id:
            evidence_args += ["--request-id", args.request_id]
        if args.user_id:
            evidence_args += ["--user-id", args.user_id]
        if args.cache_only:
            evidence_args.append("--cache-only")
        if args.use_zenmux:
            evidence_args.append("--use-zenmux")
        if args.max_model_calls is not None:
            evidence_args += ["--max-model-calls", str(args.max_model_calls)]
        if args.use_zenmux_messages:
            evidence_args.append("--use-zenmux-messages")
        return evidence_main(evidence_args)
    if args.command == "ledger":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import DATASET_DIR, EVIDENCE_OUTPUT, LEDGER_OUTPUT
        from buy_wait.event_resolution import main as ledger_main

        ledger_args = ["--dataset", str(args.dataset or DATASET_DIR)]
        evidence_path = args.evidence
        if evidence_path is None and EVIDENCE_OUTPUT.is_file():
            evidence_path = EVIDENCE_OUTPUT
        if evidence_path is not None:
            ledger_args += ["--evidence", str(evidence_path)]
        ledger_args += ["--output", str(args.output or LEDGER_OUTPUT)]
        return ledger_main(ledger_args)
    if args.command == "forecast":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import DATASET_DIR, EVIDENCE_OUTPUT, FORECAST_OUTPUT
        from buy_wait.forecast import main as forecast_main

        forecast_args = ["--dataset", str(args.dataset or DATASET_DIR), "--output", str(args.output or FORECAST_OUTPUT), "--horizon-days", str(args.horizon_days)]
        if args.evidence:
            forecast_args += ["--evidence", str(args.evidence)]
        if args.request_id:
            forecast_args += ["--request-id", args.request_id]
        if args.user_id:
            forecast_args += ["--user-id", args.user_id]
        return forecast_main(forecast_args)
    if args.command == "capacity":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import CAPACITY_OUTPUT, DATASET_DIR
        from buy_wait.capacity import main as capacity_main

        capacity_args = [
            "--dataset", str(args.dataset or DATASET_DIR),
            "--output", str(args.output or CAPACITY_OUTPUT),
            "--horizon-days", str(args.horizon_days),
        ]
        if args.evidence:
            capacity_args += ["--evidence", str(args.evidence)]
        if args.request_id:
            capacity_args += ["--request-id", args.request_id]
        if args.user_id:
            capacity_args += ["--user-id", args.user_id]
        if args.currency_unit:
            capacity_args += ["--currency-unit", args.currency_unit]
        return capacity_main(capacity_args)
    if args.command == "plan":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.candidates import main as plan_main
        from buy_wait.config import DATASET_DIR, PLANNER_OUTPUT

        plan_args = [
            "--dataset", str(args.dataset or DATASET_DIR),
            "--output", str(args.output or PLANNER_OUTPUT),
            "--horizon-days", str(args.horizon_days),
        ]
        if args.evidence:
            plan_args += ["--evidence", str(args.evidence)]
        if args.request_id:
            plan_args += ["--request-id", args.request_id]
        if args.user_id:
            plan_args += ["--user-id", args.user_id]
        return plan_main(plan_args)
    if args.command == "validate":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.candidates import load_payment_options
        from buy_wait.config import DATASET_DIR, EVIDENCE_OUTPUT, OUTPUT_PATH
        from buy_wait.event_resolution import build_ledger_from_directory
        from buy_wait.loaders import read_csv
        from buy_wait.validator import OutputValidator

        dataset = args.dataset or DATASET_DIR
        evidence_path = args.evidence or EVIDENCE_OUTPUT
        evidence = None
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        ledger = build_ledger_from_directory(dataset, evidence_path if evidence_path.is_file() else None)
        validator = OutputValidator(
            ledger,
            read_csv(Path(dataset) / "requests.csv"),
            load_payment_options(dataset),
            evidence_report=evidence,
            horizon_days=args.horizon_days,
        )
        report = validator.validate_file(args.path or OUTPUT_PATH)
        print(json.dumps({"path": str((args.path or OUTPUT_PATH).resolve()), "row_count": report.row_count, "validated_plan_count": report.validated_plan_count, "not_recommended_count": report.not_recommended_count}, sort_keys=True))
        return 0
    if args.command == "score-samples":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from buy_wait.config import CACHE_DIR, DATASET_DIR, SAMPLE_SCORE_OUTPUT
        from evaluation.sample_score import main as score_main

        score_args = [
            "--dataset", str(args.dataset or DATASET_DIR),
            "--cache", str(args.cache or CACHE_DIR / "evidence_cache.json"),
            "--output", str(args.output or SAMPLE_SCORE_OUTPUT),
            "--horizon-days", str(args.horizon_days),
        ]
        if args.samples:
            score_args += ["--samples", str(args.samples)]
        return score_main(score_args)
    if args.command == "check":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evaluation.phase7_check import main as check_main
        check_args = []
        if args.output:
            check_args += ["--output", str(args.output)]
        return check_main(check_args)
    if args.command == "package":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from evaluation.package import main as package_main
        package_args = []
        if args.output:
            package_args += ["--output", str(args.output)]
        return package_main(package_args)
    _build_parser().print_help()
    return 0


def _run_solution(args: argparse.Namespace) -> int:
    """Execute the production path for the default command or run."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from buy_wait.candidates import load_payment_options, plan_request
    from buy_wait.config import CACHE_DIR, DATASET_DIR, EVIDENCE_OUTPUT, OUTPUT_PATH, USAGE_REPORT
    from buy_wait.event_resolution import build_ledger_from_directory
    from buy_wait.evidence import run_evidence, write_report
    from buy_wait.explanations import build_decision_trace, output_row
    from buy_wait.loaders import read_csv
    from buy_wait.usage import UsageRecord, UsageTracker, write_usage_report
    from buy_wait.validator import OutputValidator, write_validated_output

    dataset = getattr(args, "dataset", None) or DATASET_DIR
    output = getattr(args, "output", None) or OUTPUT_PATH
    cache_dir = getattr(args, "cache_dir", None) or CACHE_DIR
    horizon_days = getattr(args, "horizon_days", 90)
    offline = bool(getattr(args, "offline", False))
    refresh = bool(getattr(args, "refresh_evidence", False))
    use_zenmux = bool(getattr(args, "use_zenmux", False))
    max_model_calls = getattr(args, "max_model_calls", None)
    use_zenmux_messages = bool(getattr(args, "use_zenmux_messages", False))
    evidence_path = EVIDENCE_OUTPUT
    cache_path = cache_dir / "evidence_cache.json"

    # Always run the cache-aware evidence pass so usage reflects this exact
    # output run.  Cached facts make this zero-call in the normal path.
    evidence = run_evidence(
        Path(dataset), cache_path, cache_only=offline,
        use_zenmux=use_zenmux, max_model_calls=max_model_calls,
        use_zenmux_messages=use_zenmux_messages,
    )
    write_report(evidence, evidence_path)
    ledger = build_ledger_from_directory(dataset, evidence_path if evidence_path.is_file() else None)
    requests = read_csv(Path(dataset) / "requests.csv")
    options_by_request = load_payment_options(dataset)
    plans = [
        plan_request(
            ledger,
            request,
            options=options_by_request.get(request.get("request_id", ""), ()),
            evidence_report=evidence,
            horizon_days=horizon_days,
        )
        for request in requests
    ]
    rows = [
        output_row(plan, build_decision_trace(plan, ledger, evidence_report=evidence), ledger)
        for plan in plans
    ]
    validator = OutputValidator(
        ledger,
        requests,
        options_by_request,
        evidence_report=evidence,
        horizon_days=horizon_days,
    )
    report = write_validated_output(rows, output, validator.validate_file)
    tracker = UsageTracker()
    for record in evidence.get("usage", {}).get("records", []):
        tracker.record(UsageRecord(
            provider=str(record.get("provider", "none")), model=str(record.get("model", "deterministic")),
            purpose=str(record.get("purpose", "evidence")), source_id=str(record.get("source_id", "")),
            cache_hit=bool(record.get("cache_hit", False)),
            input_tokens=int(record.get("input_tokens", 0)), output_tokens=int(record.get("output_tokens", 0)),
            estimated_cost=__import__("decimal").Decimal(str(record.get("estimated_cost", "0"))),
        ))
    mode = "offline" if offline else ("zenmux" if use_zenmux else "deterministic")
    write_usage_report(USAGE_REPORT, request_count=len(requests), tracker=tracker, mode=mode)
    print(json.dumps({
        "output": str(output.resolve()),
        "row_count": report.row_count,
        "validated_plan_count": report.validated_plan_count,
        "not_recommended_count": report.not_recommended_count,
        "usage_report": str(USAGE_REPORT.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
