from __future__ import annotations

import json
from typing import Any

from aio_fleet.public_text import assert_public_text, public_text_safe_value

SUPPORTED_COMMANDS = {
    "status",
    "blockers",
    "approvals",
    "releases",
    "upstream",
    "repo",
    "explain",
}


def render_command_response(
    *,
    command: str,
    state: dict[str, Any],
    repo: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    command = command.strip().lower()
    if command not in SUPPORTED_COMMANDS:
        raise ValueError(f"unsupported fleetbot command: {command}")
    if command == "status":
        payload = _status(state)
    elif command == "blockers":
        payload = _blockers(state)
    elif command == "approvals":
        payload = _approvals(state)
    elif command == "releases":
        payload = _releases(state)
    elif command == "upstream":
        payload = _upstream(state)
    elif command == "repo":
        payload = _repo(state, repo=repo)
    else:
        payload = _explain(state, run_id=run_id)
    response = {
        "command": command,
        "visibility": "read-only",
        "dashboard": _dashboard_anchor(command),
        **payload,
    }
    safe = public_text_safe_value(response)
    assert_public_text(json.dumps(safe, sort_keys=True), context="fleetbot response")
    return safe


def _status(state: dict[str, Any]) -> dict[str, Any]:
    summary = _summary(state)
    return {
        "title": "Fleet status",
        "posture": summary.get("posture", "unknown"),
        "summary": {
            "actions_queued": summary.get("actions_queued", 0),
            "pending_approvals": summary.get("pending_approvals", 0),
            "release_due": summary.get("release_due", 0),
            "publish_missing": summary.get("publish_missing", 0),
            "upstream_updates": summary.get("upstream_updates", 0),
            "registry_failures": summary.get("registry_failures", 0),
            "workflow_state": summary.get("workflow_state", "unknown"),
        },
    }


def _blockers(state: dict[str, Any]) -> dict[str, Any]:
    actions = [
        action
        for action in _actions(state)
        if action.get("risk") == "high" or action.get("state") == "blocked"
    ]
    failures = _failures(state)
    return {
        "title": "Fleet blockers",
        "items": actions[:10],
        "failures": failures[:10],
        "empty": not actions and not failures,
    }


def _approvals(state: dict[str, Any]) -> dict[str, Any]:
    approvals = _list(state.get("approvals"))
    return {
        "title": "Pending approvals",
        "items": approvals[:10],
        "empty": not approvals,
    }


def _releases(state: dict[str, Any]) -> dict[str, Any]:
    rows = [
        row
        for row in _list(state.get("releases"))
        if row.get("state") not in {"current", "private-skipped"}
    ]
    return {"title": "Release queue", "items": rows[:10], "empty": not rows}


def _upstream(state: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in _list(state.get("rows")) if row.get("update") is True]
    return {"title": "Upstream queue", "items": rows[:10], "empty": not rows}


def _repo(state: dict[str, Any], *, repo: str) -> dict[str, Any]:
    if not repo:
        raise ValueError("/fleet repo requires a repo name")
    items = {
        "rows": [row for row in _list(state.get("rows")) if row.get("repo") == repo],
        "releases": [
            row for row in _list(state.get("releases")) if row.get("repo") == repo
        ],
        "actions": [action for action in _actions(state) if action.get("repo") == repo],
    }
    return {
        "title": f"{repo} status",
        "repo": repo,
        "items": items,
        "empty": not any(items.values()),
    }


def _explain(state: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    if not run_id:
        raise ValueError("/fleet explain requires a run id")
    failures = [
        failure for failure in _failures(state) if str(failure.get("run_id")) == run_id
    ]
    return {
        "title": f"Run {run_id}",
        "run_id": run_id,
        "items": failures,
        "empty": not failures,
    }


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    summary = state.get("summary")
    return summary if isinstance(summary, dict) else {}


def _actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return _list(state.get("actions"))


def _failures(state: dict[str, Any]) -> list[dict[str, Any]]:
    return _list(state.get("failures"))


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dashboard_anchor(command: str) -> str:
    anchors = {
        "status": "#fleet-command-center",
        "blockers": "#current-blockers",
        "approvals": "#pending-approvals",
        "releases": "#release-queue",
        "upstream": "#upstream-queue",
        "repo": "#fleet-state",
        "explain": "#recent-failure-classifications",
    }
    return anchors.get(command, "#fleet-command-center")
