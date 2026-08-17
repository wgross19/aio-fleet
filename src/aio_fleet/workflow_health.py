from __future__ import annotations

import json
import subprocess  # nosec B404
from typing import Any

from aio_fleet.github_cli import github_cli_env

CONTROL_PLANE_WORKFLOW = "AIO Fleet Control Plane"


def control_plane_health(
    *, repo: str = "wgross19/aio-fleet", workflow: str = CONTROL_PLANE_WORKFLOW
) -> dict[str, Any]:
    result = subprocess.run(  # nosec B603 B607
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--limit",
            "5",
            "--json",
            "databaseId,status,conclusion,createdAt,updatedAt,url,event,displayTitle,headBranch",
        ],
        check=False,
        text=True,
        capture_output=True,
        env=github_cli_env(
            (
                "AIO_FLEET_DASHBOARD_TOKEN",
                "AIO_FLEET_UPSTREAM_TOKEN",
                "AIO_FLEET_CHECK_TOKEN",
                "APP_TOKEN",
                "GH_TOKEN",
                "GITHUB_TOKEN",
            )
        ),
    )
    if result.returncode != 0:
        return {
            "state": "unknown",
            "workflow": workflow,
            "repo": repo,
            "controls_enabled": False,
            "detail": (result.stderr or result.stdout).strip(),
            "runs": [],
        }
    try:
        runs = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {
            "state": "unknown",
            "workflow": workflow,
            "repo": repo,
            "controls_enabled": False,
            "detail": "invalid gh run JSON",
            "runs": [],
        }
    runs = (
        [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []
    )
    latest = runs[0] if runs else {}
    last_success = next(
        (
            run
            for run in runs
            if run.get("status") == "completed" and run.get("conclusion") == "success"
        ),
        {},
    )
    last_failure = next(
        (
            run
            for run in runs
            if run.get("status") == "completed"
            and run.get("conclusion") not in {"success", "skipped", None}
        ),
        {},
    )
    latest_state = _run_state(latest)
    return {
        "state": latest_state,
        "workflow": workflow,
        "repo": repo,
        "controls_enabled": True,
        "latest": _run_summary(latest),
        "last_success": _run_summary(last_success),
        "last_failure": _run_summary(last_failure),
        "runs": [_run_summary(run) for run in runs],
    }


def _run_state(run: dict[str, Any]) -> str:
    if not run:
        return "missing"
    if run.get("status") != "completed":
        return str(run.get("status") or "unknown").lower()
    conclusion = str(run.get("conclusion") or "unknown").lower()
    return "success" if conclusion == "success" else conclusion


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    if not run:
        return {}
    return {
        "id": run.get("databaseId", ""),
        "status": run.get("status", ""),
        "conclusion": run.get("conclusion", ""),
        "event": run.get("event", ""),
        "title": run.get("displayTitle", ""),
        "branch": run.get("headBranch", ""),
        "created_at": run.get("createdAt", ""),
        "updated_at": run.get("updatedAt", ""),
        "url": run.get("url", ""),
    }
