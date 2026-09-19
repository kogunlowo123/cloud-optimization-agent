"""Reporting: Markdown, JSON and CSV output."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from cloudopt.errors import ReportError
from cloudopt.models import CATEGORY_LABELS, Recommendation, Report
from cloudopt.security import csv_safe, md_cell, md_code

FORMATS = ("md", "json", "csv")


class _Code(str):
    """A pre-rendered inline-code table cell."""


def _cell(value: Any) -> str:
    return str(value) if isinstance(value, _Code) else md_cell(value)


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    out.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
    return out


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _rec_row(r: Recommendation) -> list[Any]:
    return [
        r.id,
        CATEGORY_LABELS[r.category],
        r.title,
        _money(r.current_monthly),
        _money(r.monthly_saving),
        r.confidence,
        r.risk,
        r.effort,
    ]


def render_markdown(report: Report) -> str:
    """The full optimization report."""
    t = report.totals
    out = [f"# Cloud optimization report as of {report.as_of.isoformat()}", ""]
    if report.summary:
        out += ["## Summary", "", report.summary, ""]

    out += ["## Savings", ""]
    out += _table(
        ["Measure", "Value"],
        [
            ["Resources", t.resources],
            ["Monthly spend in the inventory", _money(t.monthly_spend)],
            ["Identified monthly saving", f"{_money(t.identified_saving)} ({t.saving_pct}%)"],
            ["Identified annual saving", _money(t.identified_saving * 12)],
            ["Saving blocked by protection rules", _money(t.blocked_saving)],
        ],
    )
    out += [""]
    out += _table(
        ["Category", "Monthly saving"],
        [[CATEGORY_LABELS[k], _money(v)] for k, v in t.by_category.items()] or [["none", "$0.00"]],
    )
    out += [
        "",
        "Savings are applied in order (remove waste, rightsize, schedule, tier storage, commit), each against what the earlier steps leave, so they add up without double counting.",
        "",
    ]

    out += ["## Recommendations", ""]
    if report.recommendations:
        out += _table(
            [
                "Id",
                "Type",
                "Recommendation",
                "Current a month",
                "Saving a month",
                "Confidence",
                "Risk",
                "Effort",
            ],
            [_rec_row(r) for r in report.recommendations],
        )
        out += ["", "### Details", ""]
        for r in report.recommendations[:15]:
            out += [
                f"**{md_cell(r.id)} {md_cell(r.title)}**",
                "",
                md_cell(r.rationale),
                "",
                f"Action: {md_cell(r.action)}.",
                "",
            ]
            out += [f"- Check: {md_cell(c)}" for c in r.checks]
            if r.commands:
                out += [
                    "",
                    "Suggested commands (not run by this tool):",
                    "",
                    "```bash",
                    *[c.replace("```", "'''") for c in r.commands],
                    "```",
                ]
            out.append("")
    else:
        out += ["No recommendations.", ""]

    if report.blocked:
        out += ["## Blocked by protection rules", ""]
        out += _table(
            ["Id", "Recommendation", "Would save a month", "Reason"],
            [[r.id, r.title, _money(r.monthly_saving), r.blocked] for r in report.blocked],
        )
        out.append("")

    out += ["## Cost anomalies", ""]
    if report.anomalies:
        out += _table(
            [
                "Service",
                "Kind",
                "From",
                "Baseline a day",
                "Now a day",
                "Extra a month",
                "Largest contributors",
            ],
            [
                [
                    a.service,
                    a.kind,
                    a.first_day.isoformat(),
                    _money(a.baseline_daily),
                    _money(a.current_daily),
                    _money(a.extra_monthly),
                    _Code(
                        md_code(
                            ", ".join(
                                f"{x['resource_id']} (+${x['extra_daily']:.2f}/day)"
                                for x in a.top_resources
                            )
                            or "none"
                        )
                    ),
                ]
                for a in report.anomalies
            ],
        )
    else:
        out.append("None detected.")
    out.append("")

    out += ["## Cost allocation", ""]
    if report.allocation:
        for a in report.allocation:
            out += [f"By `{md_cell(a.dimension)}` ({a.untagged_share:.1%} untagged):", ""]
            out += _table(
                ["Value", "Monthly cost", "Share"],
                [[row.value, _money(row.monthly_cost), f"{row.share:.1%}"] for row in a.rows[:10]],
            )
            out.append("")
    else:
        out += ["No billing data was supplied.", ""]

    if report.warnings:
        out += ["## Data warnings", ""] + [f"- {md_cell(w)}" for w in report.warnings] + [""]
    return "\n".join(out)


def render_json(report: Report) -> str:
    return report.model_dump_json(indent=2)


def render_csv(report: Report) -> str:
    """One row per recommendation, with spreadsheet formulas neutralised."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "id",
            "category",
            "resource_id",
            "resource_name",
            "provider",
            "title",
            "current_monthly",
            "monthly_saving",
            "annual_saving",
            "confidence",
            "risk",
            "effort",
            "blocked",
        ]
    )
    for r in [*report.recommendations, *report.blocked]:
        writer.writerow(
            [
                csv_safe(v)
                for v in (
                    r.id,
                    r.category,
                    r.resource_id,
                    r.resource_name,
                    r.provider,
                    r.title,
                    r.current_monthly,
                    r.monthly_saving,
                    r.annual_saving,
                    r.confidence,
                    r.risk,
                    r.effort,
                    r.blocked,
                )
            ]
        )
    return buffer.getvalue()


def write_reports(report: Report, out_dir: Path, formats: list[str]) -> list[Path]:
    """Write ``report.<ext>`` for each format."""
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise ReportError(f"unknown format {unknown[0]!r}; choose from {', '.join(FORMATS)}")
    renderers = {"md": render_markdown, "json": render_json, "csv": render_csv}
    paths: list[Path] = []
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for fmt in formats:
            path = out_dir / f"report.{fmt}"
            path.write_text(renderers[fmt](report), encoding="utf-8")
            paths.append(path)
    except OSError as exc:
        raise ReportError(f"cannot write to {out_dir}: {exc}") from exc
    return paths
