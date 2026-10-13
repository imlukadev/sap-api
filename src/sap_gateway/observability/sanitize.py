"""Redação de dados sensíveis antes de qualquer saída de telemetria (logs, traces, exceções)."""

from __future__ import annotations

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"
MAX_TEXT_LENGTH = 2048
MAX_STACKTRACE_LENGTH = 8192

SENSITIVE_KEY_RE = re.compile(
    r"pass(word|wd)?|pwd|secret|token|authorization|cookie|api[-_]?key|credential|session",
    re.IGNORECASE,
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)((?:proxy-)?authorization\s*[:=]\s*)(?:(?:basic|bearer|digest)\s+)?[^\s,;\"']+"
        ),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"(?i)\b(basic|bearer)\s+[A-Za-z0-9+/=._~-]{8,}"), rf"\1 {REDACTED}"),
    (
        re.compile(
            r"(?i)((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
        ),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(r"(?is)(<(?:[\w.-]+:)?(?:password|token|secret)\b[^>]*>)[^<]*(</)"),
        rf"\1{REDACTED}\2",
    ),
    (re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[^\r\n]+"), rf"\1{REDACTED}"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), rf"\1{REDACTED}@"),
)


def redact_text(text: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = f"{text[:limit]}…[truncated]"
    return text


def is_sensitive_key(key: str) -> bool:
    return SENSITIVE_KEY_RE.search(key) is not None


def sanitize_value(key: str, value: object) -> str | int | float | bool:
    """Reduz um valor a um primitivo seguro para atributo de log/span."""
    if is_sensitive_key(key):
        return REDACTED
    if isinstance(value, bool | int | float):
        return value
    return redact_text(str(value))


def sanitize_mapping(data: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    return {key: sanitize_value(key, value) for key, value in data.items()}
