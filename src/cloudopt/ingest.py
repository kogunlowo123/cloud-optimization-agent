"""Loading inventory and billing exports (JSON Lines) into typed records.

Both formats are the tool's own simple schema, described in the README. Producing them from a provider export
(a cost and usage report, an inventory API) is a small mapping job that belongs outside this tool. Bad lines
are counted and described without echoing their content.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cloudopt.errors import DataError
from cloudopt.models import BillingLine, IngestReport, Resource


def _load(
    lines: Iterable[str], kind: str, build: Any, max_line_bytes: int, max_records: int
) -> tuple[list[Any], IngestReport]:
    report = IngestReport(kind=kind)
    records: list[Any] = []
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        report.lines += 1
        if report.lines > max_records:
            raise DataError(f"{kind} input exceeds the limit of {max_records} records")

        def reject(reason: str, line: int = number) -> None:
            report.rejected += 1
            if len(report.errors) < 20:
                report.errors.append(f"line {line}: {reason}")

        if len(raw.encode("utf-8", errors="replace")) > max_line_bytes:
            reject(f"line exceeds {max_line_bytes} bytes")
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            reject("not valid JSON")
            continue
        if not isinstance(data, dict):
            reject("not a JSON object")
            continue
        try:
            records.append(build(data))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "record"
            reject(f"{where}: {first['msg']}")
        except (ValueError, TypeError):
            reject("record does not match the expected format")
        else:
            report.accepted += 1
    return records, report


def parse_inventory(
    lines: Iterable[str], *, max_line_bytes: int = 100_000, max_records: int = 3_000_000
) -> tuple[list[Resource], IngestReport]:
    """Inventory records. Duplicate ids keep the first occurrence and are reported."""
    resources, report = _load(
        lines, "inventory", Resource.model_validate, max_line_bytes, max_records
    )
    seen: set[str] = set()
    unique: list[Resource] = []
    for resource in resources:
        if resource.id in seen:
            report.errors.append(f"duplicate resource id {resource.id!r} ignored")
            report.accepted -= 1
            report.rejected += 1
            continue
        seen.add(resource.id)
        unique.append(resource)
    return unique, report


def parse_billing(
    lines: Iterable[str], *, max_line_bytes: int = 100_000, max_records: int = 3_000_000
) -> tuple[list[BillingLine], IngestReport]:
    """Daily billing lines."""
    return _load(lines, "billing", BillingLine.model_validate, max_line_bytes, max_records)


def read_lines(path: Path) -> list[str]:
    """Read a text file into lines.

    Raises:
        DataError: If the file cannot be read.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DataError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
