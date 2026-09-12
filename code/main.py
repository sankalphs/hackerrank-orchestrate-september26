"""Command-line entry point for the Buy or Wait? solution."""

from __future__ import annotations

import argparse
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
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
    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
