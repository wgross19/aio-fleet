from __future__ import annotations

import re
from typing import Any

_PUBLIC_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("macOS home path", re.compile(r"/Users/[^`\s)>'\"]+")),
    ("Linux home path", re.compile(r"/home/[^`\s)>'\"]+")),
    ("Windows user path", re.compile(r"[A-Za-z]:\\Users\\[^`\s)>'\"]+")),
    ("Codex worktree path", re.compile(r"\.codex/worktrees/[^`\s)>'\"]*")),
    ("local virtualenv executable", re.compile(r"\.venv/(?:bin|Scripts)/[^\s`]+")),
    (
        "Discord webhook URL",
        re.compile(
            r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[^\s`]+"
        ),
    ),
    ("webhook URL path", re.compile(r"https?://[^\s`]+/api/webhooks/[^\s`]+")),
)


def public_text_findings(text: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in _PUBLIC_TEXT_PATTERNS:
        if pattern.search(text):
            findings.append(label)
    return findings


def redact_public_text(text: str) -> str:
    redacted = text
    for label, pattern in _PUBLIC_TEXT_PATTERNS:
        redacted = pattern.sub(f"<redacted: {label}>", redacted)
    return redacted


def public_text_safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_public_text(value)
    if isinstance(value, list):
        return [public_text_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [public_text_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {
            (
                redact_public_text(key) if isinstance(key, str) else key
            ): public_text_safe_value(item)
            for key, item in value.items()
        }
    return value


def assert_public_text(text: str, *, context: str = "public text") -> None:
    findings = public_text_findings(text)
    if findings:
        raise ValueError(
            f"{context} contains non-public local or secret-like content: "
            + ", ".join(findings)
        )
