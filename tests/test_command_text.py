from __future__ import annotations

import pytest

from aio_fleet.command_text import fleet_command, fleet_command_args


def test_fleet_command_uses_uv_console_script() -> None:
    assert fleet_command("release", "status", "--repo", "sure-aio") == (  # nosec B101
        "uv run aio-fleet release status --repo sure-aio"
    )


def test_fleet_command_preserves_placeholders() -> None:
    assert fleet_command(
        "control-check", "--sha", "<sha>", "--dry-run"
    ) == (  # nosec B101
        "uv run aio-fleet control-check --sha <sha> --dry-run"
    )


def test_fleet_command_quotes_whitespace() -> None:
    assert fleet_command(
        "validate-repo", "--repo-path", "../repo with spaces"
    ) == (  # nosec B101
        "uv run aio-fleet validate-repo --repo-path '../repo with spaces'"
    )


def test_fleet_command_rejects_control_text() -> None:
    with pytest.raises(ValueError, match="control text"):
        fleet_command("status\nwhoami")


def test_fleet_command_args_are_unquoted_argv() -> None:
    assert fleet_command_args(
        "registry", "verify", "--repo", "sure-aio"
    ) == [  # nosec B101
        "uv",
        "run",
        "aio-fleet",
        "registry",
        "verify",
        "--repo",
        "sure-aio",
    ]
