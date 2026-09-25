"""Identifiers written inside free-text values.

No patient identifier may reach the model; identifiers stay on the case record.
The normalizer drops identifier fields by name, but it cannot see a national ID
a nurse typed into a text field. These patterns can. Two places use them: the
payload builder redacts with them, and the no-identifiers verifier scans again
with them and halts the case if anything survived.

Matching is fixed regex. A value is redacted because it matches a pattern, never
because a model guessed it looked like an identifier.
"""

from __future__ import annotations

import re
from typing import Any

# Order matters: a 9-digit landline (`021234567`) is also nine digits, so phone
# is tried before national_id and wins. Digit look-arounds stop a longer number
# (an order or lab code) from matching in its middle.
# Phone: mobile 05X, VoIP 07X or a one-digit landline prefix, after 0 or 972
# (with or without +), grouped 3-4 or 3-2-2. National ID: nine digits, or
# written with its check digit split off (12345678-9, 12-345678-9).
_PATTERNS = (
    ("phone", re.compile(r"(?<![\d+])(?:\+?972[- ]?0?|0)(?:5\d|7\d|[2-489])"
                         r"[- ]?\d{3}[- ]?\d{2}[- ]?\d{2}(?!\d)"), "[REDACTED_PHONE]"),
    # `(?<!\d\.)`: the digits after a decimal point are not an ID, but an ID
    # after an abbreviation dot (`No.123456789`) still is.
    ("national_id", re.compile(r"(?<!\d)(?<!\d\.)(?:\d{9}|\d{8}-\d|\d{2}-\d{6}-\d)(?!\d)"),
     "[REDACTED_ID]"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
)


def _children(value: Any, path: str):
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.{k}" if path else str(k), v
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield f"{path}[{i}]", v


def find_identifiers(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every (path, kind) where a string value holds an identifier."""
    if isinstance(value, str):
        found, rest = [], value
        for kind, pattern, token in _PATTERNS:
            if pattern.search(rest):
                found.append((path, kind))
            rest = pattern.sub(token, rest)   # so a phone isn't also reported as an ID
        return found
    return [hit for child_path, child in _children(value, path)
            for hit in find_identifiers(child, child_path)]


def redact_identifiers(value: Any) -> Any:
    """A copy of `value` with every identifier in its strings replaced."""
    if isinstance(value, str):
        for _, pattern, token in _PATTERNS:
            value = pattern.sub(token, value)
        return value
    if isinstance(value, dict):
        return {k: redact_identifiers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_identifiers(v) for v in value]
    return value
