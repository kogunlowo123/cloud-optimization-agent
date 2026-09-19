# Security Policy

## Supported versions

Security fixes are released for the latest minor version on the `main` branch.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a vulnerability

Do not open a public issue for security reports. Use GitHub's private vulnerability reporting (the
**Report a vulnerability** button on this repository's **Security** tab) and include a description and
impact, the affected version or commit, and a minimal reproduction. Please remove real account data first.
You can expect an acknowledgement within 3 business days and a triage decision within 10 business days.

## Trust boundary

| Input | Trust |
| ----- | ----- |
| Inventory and billing files | Untrusted. Resource names, ids and tags can contain anything |
| Policy and pricing files | Trusted operator input, parsed safely and validated |
| Model output used for summaries | Untrusted text, accepted only if grounded in supplied figures |

## Security controls

| Threat | Control | Location |
| ------ | ------- | -------- |
| Command injection through suggested commands | Every inventory value is stripped of control characters and shell-quoted. Commands are text only and are never executed | `commands.py` |
| Acting on protected resources | Tagged or listed resources are never planned and are reported as blocked | `planner.py` |
| Malformed or oversized input | Per-line byte limit, total record limit, strict schemas, bounded tags and fields | `ingest.py`, `models.py` |
| Leaky error messages | Rejected lines report a line number and the field, never content. CLI errors are redacted | `ingest.py`, `cli.py` |
| Code execution through configuration | `yaml.safe_load` only, 2 MB limit, strict models that forbid unknown fields | `config.py`, `pricing.py` |
| Markup injection into reports | Table cells escaped, evidence in sanitised code spans, HTML characters encoded | `security.py`, `reporting.py` |
| Spreadsheet formula injection in CSV | Cells starting with `=`, `+`, `-`, `@`, tab or carriage return get a leading quote | `security.csv_safe` |
| Prompt injection into summaries | The model receives only aggregate figures. Output containing numbers absent from them is discarded | `summary.py` |
| Vulnerable dependencies | `pip-audit`, Dependabot, CodeQL | `.github/` |

## Known limits

- The tool never calls a cloud API, so it has no credentials to protect and cannot verify inventory accuracy.
- Reports list resource names, tags and costs. Handle them like any internal financial record.
