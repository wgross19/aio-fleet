from __future__ import annotations

import json
import subprocess

from aio_fleet import workflow_health


def test_control_plane_health_uses_dashboard_token(monkeypatch) -> None:
    captured_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object):
        nonlocal captured_env
        captured_env = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 123,
                        "status": "completed",
                        "conclusion": "success",
                        "createdAt": "2026-05-13T00:00:00Z",
                        "updatedAt": "2026-05-13T00:01:00Z",
                        "url": "https://github.com/wgross19/aio-fleet/actions/runs/123",
                        "event": "schedule",
                        "displayTitle": "AIO Fleet Control Plane",
                        "headBranch": "main",
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setenv("AIO_FLEET_DASHBOARD_TOKEN", "dashboard-token")
    monkeypatch.setenv("AIO_FLEET_UPSTREAM_TOKEN", "upstream-token")
    monkeypatch.setenv("GH_TOKEN", "lower-priority-token")
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setattr(workflow_health.subprocess, "run", fake_run)

    result = workflow_health.control_plane_health()

    assert result["state"] == "success"  # nosec B101
    assert captured_env["GH_TOKEN"] == "dashboard-token"  # nosec B101
    assert "AIO_FLEET_DASHBOARD_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101
