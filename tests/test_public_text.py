from __future__ import annotations

import pytest

from aio_fleet.public_text import (
    assert_public_text,
    public_text_findings,
    public_text_safe_value,
)


def test_public_text_accepts_repo_relative_validation_commands() -> None:
    assert (  # nosec B101
        public_text_findings("python -m pytest tests/test_alpha_lane_assets.py") == []
    )


@pytest.mark.parametrize(
    "text",
    [
        "/Users/shadowbook/Documents/sure-aio/.venv/bin/python -m pytest",
        "/home/shadowbook/aio-fleet/.venv/bin/python -m pytest",
        "C:\\Users\\shadowbook\\repo\\.venv\\Scripts\\python.exe -m pytest",
        "https://discord.com/api/webhooks/123/secret",
    ],
)
def test_public_text_rejects_local_paths_and_webhooks(text: str) -> None:
    with pytest.raises(ValueError):
        assert_public_text(text, context="PR body")


def test_public_text_safe_value_redacts_nested_strings() -> None:
    value = {
        "body": [
            "/Users/shadowbook/Documents/aio-fleet/.venv/bin/python",
            {"webhook": "https://discord.com/api/webhooks/123/secret"},
        ]
    }

    safe = public_text_safe_value(value)

    assert safe == {  # nosec B101
        "body": [
            "<redacted: macOS home path>",
            {"webhook": "<redacted: Discord webhook URL>"},
        ]
    }
