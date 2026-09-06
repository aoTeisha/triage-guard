"""Prompt-injection detection (docs/SPECIFICATION.md § Guards).

Runs BEFORE any model sees the text — the spec's forbidden sequence is
"injection reaches the model", so a probabilistic detector cannot be the one
enforcing it.
"""

from __future__ import annotations

import re

# Injection patterns, each tagged with the category written to the audit log.
_I = re.IGNORECASE | re.MULTILINE

_INJECTION_PATTERNS = (
    (re.compile(r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)", _I), "override"),
    (re.compile(r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)", _I), "override"),
    (re.compile(r"new\s+instructions\s*:", _I), "override"),
    (re.compile(r"you\s+are\s+now\b", _I), "role_injection"),
    (re.compile(r"^\s*(?:system|assistant)\s*:", _I), "role_injection"),
    (re.compile(r"```", _I), "format_break"),
    (re.compile(r"</?(?:system|prompt|instructions?)>", _I), "format_break"),
    (re.compile(r"\bset\s+(?:the\s+)?acuity\s+to\s*\d", _I), "acuity_targeting"),
    (re.compile(r"\bacuity\s*(?:=|:)\s*\d", _I), "acuity_targeting"),
)


def detect_injection(free_text: str) -> tuple[bool, str | None]:
    """Returns (is_injection, matched_category). The category feeds the audit
    log — explainability, not just a boolean.
    """
    if not free_text:
        return False, None
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(free_text):
            return True, label
    return False, None
