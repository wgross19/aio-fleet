from __future__ import annotations

import os

GITHUB_CLI_TOKEN_KEYS = (
    "AIO_FLEET_DASHBOARD_TOKEN",
    "AIO_FLEET_UPSTREAM_TOKEN",
    "AIO_FLEET_ISSUE_TOKEN",
    "AIO_FLEET_WORKFLOW_TOKEN",
    "AIO_FLEET_CHECK_TOKEN",
    "APP_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def github_cli_env(
    keys: tuple[str, ...] = GITHUB_CLI_TOKEN_KEYS,
) -> dict[str, str] | None:
    token = github_cli_token(keys)
    if not token:
        return None
    env = os.environ.copy()
    for key in GITHUB_CLI_TOKEN_KEYS:
        env.pop(key, None)
    env["GH_TOKEN"] = token
    return env


def github_cli_token(keys: tuple[str, ...] = GITHUB_CLI_TOKEN_KEYS) -> str:
    for key in keys:
        token = os.environ.get(key, "").strip()
        if token:
            return token
    return ""
