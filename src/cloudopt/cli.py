"""Command-line interface ``cloudopt``.

Exit codes: 0 success, 1 a gate failed (``--fail-above-saving-pct``), 2 invalid input or a runtime error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from cloudopt.config import Settings, load_policy
from cloudopt.container import build_service
from cloudopt.errors import OptError
from cloudopt.logging_setup import configure_logging
from cloudopt.pricing import load_pricing
from cloudopt.reporting import FORMATS, render_csv, render_json, render_markdown, write_reports
from cloudopt.security import redact
from cloudopt.simulate import simulate, write_records


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {text!r}; use YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloudopt", description="Cloud optimization agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sim = sub.add_parser(
        "simulate", help="write a synthetic multi-cloud inventory and 30 days of billing"
    )
    sim.add_argument("--out", type=Path, required=True)
    sim.add_argument("--today", type=_date, help="reference date, YYYY-MM-DD (default: today)")
    sim.add_argument("--seed", type=int, default=7)

    analyze = sub.add_parser("analyze", help="find savings in an inventory and billing export")
    analyze.add_argument("--inventory", type=Path, required=True, help="JSON Lines inventory")
    analyze.add_argument("--billing", type=Path, help="JSON Lines daily billing")
    analyze.add_argument(
        "--pricing", type=Path, help="pricing YAML (default: CLOUDOPT_PRICING_FILE)"
    )
    analyze.add_argument("--policy", type=Path, help="policy YAML (default: CLOUDOPT_POLICY_FILE)")
    analyze.add_argument("--today", type=_date, help="reference date, YYYY-MM-DD (default: today)")
    analyze.add_argument(
        "--out", type=Path, help="write report files here instead of printing Markdown"
    )
    analyze.add_argument(
        "--format", default="md,json,csv", help=f"comma list from: {', '.join(FORMATS)}"
    )
    analyze.add_argument(
        "--fail-above-saving-pct",
        type=float,
        help="exit 1 if identified savings exceed this percentage of spend",
    )

    sub.add_parser("catalog", help="list the instance types in the price catalogue")
    return parser


def _cmd_catalog(pricing_file: Path | None) -> int:
    pricing = load_pricing(pricing_file)
    print(
        f"{'provider':<8} {'type':<18} {'family':<14} {'vcpu':>4} {'mem GB':>7} {'$/hour':>8}  successor"
    )
    for (provider, name), item in sorted(pricing.instances.items()):
        print(
            f"{provider:<8} {name:<18} {item.family:<14} {item.vcpu:>4} {item.mem_gb:>7g} {item.hourly:>8.4f}  {item.successor or '-'}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = _parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        settings = Settings()
        configure_logging(settings.log_level, json_output=settings.log_json)
        if args.command == "catalog":
            return _cmd_catalog(settings.pricing_file)
        if args.command == "simulate":
            today = args.today or datetime.now(timezone.utc).date()
            for path in write_records(simulate(today=today, seed=args.seed), args.out):
                print(f"wrote {path}")
            return 0

        service = build_service(settings)
        resources, billing, reports = service.load(args.inventory, args.billing)
        for ingest in reports:
            note = f", {ingest.rejected} rejected" if ingest.rejected else ""
            print(f"loaded {ingest.accepted} {ingest.kind} records{note}", file=sys.stderr)
        report = service.analyze(
            resources,
            billing,
            policy=load_policy(args.policy or settings.policy_file),
            pricing=load_pricing(args.pricing or settings.pricing_file),
            today=args.today,
        )
        formats = [f.strip() for f in args.format.split(",") if f.strip()]
        if args.out:
            for path in write_reports(report, args.out, formats):
                print(f"wrote {path}")
            print(report.summary)
        elif formats == ["json"]:
            print(render_json(report))
        elif formats == ["csv"]:
            print(render_csv(report), end="")
        else:
            print(render_markdown(report))
        if (
            args.fail_above_saving_pct is not None
            and report.totals.saving_pct > args.fail_above_saving_pct
        ):
            return 1
        return 0
    except (OptError, ValidationError) as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
