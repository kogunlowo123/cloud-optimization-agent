"""Secret redaction and output escaping."""

from __future__ import annotations

import re

REDACTION = "[REDACTED]"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END "
        r"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    re.compile(
        r"(?i)(?:^|[\s\"'/-])(?:password|passwd|pwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"]?"
        r"(?!\[REDACTED\])[^\s'\",;]{4,}"
    ),
)
_ARG_SECRET = re.compile(
    r"(?i)(?P<flag>(?:--?)(?:password|passwd|pwd|pass|token|secret|apikey|api-key))(?P<sep>[\s=:]+)"
    r"(?P<value>(?!\[REDACTED\])[^\s'\"]+)"
)


def redact(text: str) -> str:
    """Replace credential-shaped substrings, including secrets passed as command-line arguments."""
    text = _ARG_SECRET.sub(lambda m: f"{m['flag']}{m['sep']}{REDACTION}", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_replace, text)
    return text


def _replace(match: re.Match[str]) -> str:
    value = match.group(0)
    key = re.search(r"(?i)(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]", value)
    if key:
        return f"{value[: key.end()]} {REDACTION}"
    return REDACTION


def md_cell(value: object) -> str:
    """Make ``value`` safe inside a Markdown table cell (no pipes, markup or line breaks)."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("|", "\\|").strip()


def md_code(value: object) -> str:
    """Inline code for a Markdown table cell. Backticks, pipes and line breaks are neutralised."""
    text = " ".join(str(value).replace("`", "'").split())
    return "`" + text.replace("|", "\\|") + "`"


def csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection by prefixing risky cells with a quote."""
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
