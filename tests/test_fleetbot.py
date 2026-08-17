from __future__ import annotations

import pytest

from aio_fleet.fleetbot import render_command_response


def test_fleetbot_status_is_read_only() -> None:
    response = render_command_response(
        command="status",
        state={
            "summary": {
                "posture": "green",
                "actions_queued": 0,
                "pending_approvals": 0,
                "workflow_state": "success",
            }
        },
    )

    assert response["visibility"] == "read-only"  # nosec B101
    assert response["dashboard"] == "#fleet-command-center"  # nosec B101
    assert response["posture"] == "green"  # nosec B101


def test_fleetbot_repo_filters_report_state() -> None:
    response = render_command_response(
        command="repo",
        repo="sure-aio",
        state={
            "rows": [{"repo": "sure-aio", "component": "aio", "update": False}],
            "releases": [{"repo": "other-aio", "state": "current"}],
            "actions": [{"repo": "sure-aio", "kind": "registry-publish"}],
        },
    )

    assert response["repo"] == "sure-aio"  # nosec B101
    assert len(response["items"]["rows"]) == 1  # nosec B101
    assert response["items"]["releases"] == []  # nosec B101
    assert len(response["items"]["actions"]) == 1  # nosec B101


def test_fleetbot_explain_requires_run_id() -> None:
    with pytest.raises(ValueError, match="run id"):
        render_command_response(command="explain", state={})
