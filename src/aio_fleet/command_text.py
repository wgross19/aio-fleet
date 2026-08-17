from __future__ import annotations

import re
import shlex
from typing import Any

FLEET_COMMAND_PREFIX = ("uv", "run", "aio-fleet")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER_RE = re.compile(r"^<[A-Za-z0-9_.-]+>$")


def fleet_command_args(*parts: Any) -> list[str]:
    """Build argv for an operator-facing local aio-fleet command."""

    return [*FLEET_COMMAND_PREFIX, *[_command_part(part) for part in parts]]


def fleet_command(*parts: Any) -> str:
    """Build copy/pasteable operator-facing local aio-fleet command text."""

    return " ".join(_quote_command_part(part) for part in fleet_command_args(*parts))


def _command_part(part: Any) -> str:
    value = str(part)
    if _CONTROL_RE.search(value):
        raise ValueError(f"unsafe command part contains control text: {value!r}")
    return value


def _quote_command_part(part: str) -> str:
    if _PLACEHOLDER_RE.fullmatch(part):
        return part
    return shlex.quote(part)
