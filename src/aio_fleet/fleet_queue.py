from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any

from aio_fleet.command_text import fleet_command_args
from aio_fleet.public_text import assert_public_text, public_text_safe_value

ALLOWED_WORKFLOWS = {"control-plane.yml"}
ALLOWED_WORKFLOW_INPUTS = {
    "mode",
    "repo",
    "sha",
    "event",
    "publish",
    "publish_component",
    "dry_run",
}
SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
SAFE_REPO_LABEL_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}(?:/[A-Za-z0-9_.-]{1,100})?")
SAFE_RUN_ID_RE = re.compile(r"[0-9]{1,30}")
ACTIONABLE_RELEASE_STATES = {
    "publish-missing",
    "release-due",
    "catalog-sync-needed",
    "blocked",
}
REGISTRY_PUBLISH_STATES = {"publish-missing"}
RELEASE_TRANSACTION_STATES = {"release-due", "blocked"}
CATALOG_SYNC_STATES = {"catalog-sync-needed"}


def build_action_queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic command-center actions from a fleet report state."""

    actions: list[dict[str, Any]] = []
    actions.extend(_release_actions(list(_dicts(state.get("releases")))))
    actions.extend(_upstream_actions(list(_dicts(state.get("rows")))))
    actions.extend(_catalog_actions(list(_dicts(state.get("destination_repos")))))
    actions.extend(_standards_actions(list(_dicts(state.get("cleanup")))))
    actions.extend(_failure_retry_actions(list(_dicts(state.get("failures")))))
    return _dedupe_actions(actions)


def pending_approvals_from_actions(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    approvals: list[dict[str, Any]] = []
    for action in actions:
        if action.get("kind") != "registry-publish":
            continue
        dispatch = action.get("workflow_dispatch")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        inputs = dispatch.get("inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        if inputs.get("mode") != "control-check" or inputs.get("publish") != "true":
            continue
        approvals.append(
            {
                "id": action["id"],
                "repo": action.get("repo", ""),
                "component": action.get("component", ""),
                "kind": action.get("kind", ""),
                "risk": action.get("risk", "medium"),
                "target_sha": action.get("target_sha", ""),
                "state": "queued",
                "provenance": "operator-action",
                "evidence": action.get("evidence", {}),
                "workflow_dispatch": dispatch,
                "next_action": (
                    "approve the protected registry-publish job only after the "
                    "repo/component/SHA context matches the queue entry"
                ),
            }
        )
    return approvals


def catalog_readiness_from_state(state: dict[str, Any]) -> dict[str, Any]:
    release_rows = list(_dicts(state.get("releases")))
    destination_rows = list(_dicts(state.get("destination_repos")))
    sync_needed = [
        {
            "repo": row.get("repo", ""),
            "component": row.get("component", "aio"),
            "state": row.get("state", ""),
            "next_action": row.get("next_action", ""),
        }
        for row in release_rows
        if row.get("catalog_sync_needed") is True
        or row.get("state") == "catalog-sync-needed"
    ]
    destination_findings = [
        {
            "repo": row.get("repo", ""),
            "catalog_state": row.get("catalog_state", ""),
            "findings": row.get("catalog_findings", []),
        }
        for row in destination_rows
        if str(row.get("catalog_state", "ok")) not in {"", "ok", "private-skipped"}
    ]
    state_label = "ready" if not sync_needed and not destination_findings else "drift"
    return {
        "state": state_label,
        "sync_needed": sync_needed,
        "destination_findings": destination_findings,
        "next_action": (
            "none"
            if state_label == "ready"
            else "run catalog sync dry-run and open a catalog PR for source metadata changes"
        ),
    }


def standards_drift_from_state(state: dict[str, Any]) -> dict[str, Any]:
    cleanup_rows = list(_dicts(state.get("cleanup")))
    drift_rows = [
        {
            "repo": row.get("repo", ""),
            "findings_count": row.get("findings_count", 0),
            "findings": row.get("findings", []),
        }
        for row in cleanup_rows
        if row.get("state") == "drift" and row.get("provenance") != "local-only"
    ]
    return {
        "state": "ok" if not drift_rows else "drift",
        "repos": drift_rows,
        "next_action": (
            "none"
            if not drift_rows
            else "run standards reconcile in dry-run mode and review the generated PRs"
        ),
    }


def candidate_lane_from_state(_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "planning",
        "candidates": [],
        "required_bootstrap": [
            "source project and license reviewed",
            "runtime services mapped to Unraid-first defaults",
            "template metadata drafted",
            "registry/package naming chosen",
            "support thread and CA-readiness checklist prepared",
        ],
        "next_action": (
            "track new AIO candidates here after command-center drift gates are stable"
        ),
    }


def enrich_command_center_state(state: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(state)
    failures = list(_dicts(enriched.get("failures")))
    enriched["failures"] = failures
    actions = build_action_queue(enriched)
    enriched["actions"] = actions
    enriched["approvals"] = pending_approvals_from_actions(actions)
    enriched["catalog"] = catalog_readiness_from_state(enriched)
    enriched["standards"] = standards_drift_from_state(enriched)
    enriched["candidates"] = candidate_lane_from_state(enriched)
    summary = dict(enriched.get("summary", {}))
    summary.update(
        {
            "actions_queued": len(actions),
            "pending_approvals": len(enriched["approvals"]),
            "failure_classifications": len(failures),
            "catalog_state": enriched["catalog"]["state"],
            "standards_state": enriched["standards"]["state"],
        }
    )
    enriched["summary"] = summary
    safe = public_text_safe_value(enriched)
    assert_public_text(json.dumps(safe, sort_keys=True), context="fleet queue state")
    return safe


def action_by_id(actions: list[dict[str, Any]], action_id: str) -> dict[str, Any]:
    for action in actions:
        if action.get("id") == action_id:
            return action
    raise KeyError(f"unknown queue action id: {action_id}")


def dispatch_plan(action: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    if not dry_run:
        raise RuntimeError(
            "fleet-queue dispatch currently supports dry-run only; protected "
            "workflow dispatch remains an explicit operator action"
        )
    dispatch = action.get("workflow_dispatch")
    command = ""
    if isinstance(dispatch, dict) and dispatch:
        command = _gh_workflow_command(dispatch, force_dry_run=True)
    return {
        "action": action,
        "dry_run": True,
        "would_dispatch": bool(command),
        "command": command,
        "requires_approval": bool(action.get("requires_approval")),
    }


def _release_actions(release_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in release_rows:
        state = str(row.get("state", ""))
        if state not in ACTIONABLE_RELEASE_STATES:
            continue
        if state == "private-skipped":
            continue
        repo = _safe_name(row.get("repo", ""))
        component = _safe_name(row.get("component", "aio") or "aio")
        if repo is None or component is None:
            continue
        sha = _safe_sha(row.get("sha", ""))
        if state in REGISTRY_PUBLISH_STATES:
            failures = [
                str(failure)
                for failure in row.get("registry_failures", [])
                if str(failure).strip()
            ]
            if not row.get("registry_verified") or not failures:
                continue
            actions.append(
                _action(
                    kind="registry-publish",
                    repo=repo,
                    component=component,
                    state="queued",
                    risk="medium",
                    requires_approval=True,
                    source="release-plan",
                    target_sha=sha,
                    provenance="remote-confirmed",
                    evidence={
                        "registry_failures": failures,
                        "registry_failure_evidence": row.get(
                            "registry_failure_evidence", []
                        ),
                        "registry_tags": row.get("registry_tags", {}),
                    },
                    next_command_args=_release_transaction_command(
                        repo, component, sha
                    ),
                    workflow_dispatch=_registry_publish_dispatch(
                        repo, component=component, sha=sha
                    ),
                )
            )
        elif state in RELEASE_TRANSACTION_STATES:
            actions.append(
                _action(
                    kind="release-transaction",
                    repo=repo,
                    component=component,
                    state="blocked" if state == "blocked" else "queued",
                    risk="high" if state == "blocked" else "medium",
                    requires_approval=True,
                    source="release-plan",
                    target_sha=sha,
                    provenance=str(row.get("provenance", "operator-action")),
                    evidence={"blockers": row.get("blockers", [])},
                    next_command_args=_release_transaction_command(
                        repo, component, sha
                    ),
                    workflow_dispatch=(
                        _registry_publish_dispatch(repo, component=component, sha=sha)
                        if sha
                        else {}
                    ),
                )
            )
        elif state in CATALOG_SYNC_STATES:
            actions.append(
                _action(
                    kind="catalog-sync",
                    repo=repo,
                    component=component,
                    state="queued",
                    risk="low",
                    requires_approval=False,
                    source="release-plan",
                    target_sha=sha,
                    provenance=str(row.get("provenance", "operator-action")),
                    evidence={"warnings": row.get("warnings", [])},
                    next_command_args=_catalog_sync_command(repo),
                    workflow_dispatch={},
                )
            )
    return actions


def _upstream_actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in rows:
        if row.get("update") is not True:
            continue
        repo = _safe_name(row.get("repo", ""))
        component = _safe_name(row.get("component", "aio") or "aio")
        if repo is None or component is None:
            continue
        state = "blocked" if row.get("safety") == "blocked" else "queued"
        actions.append(
            _action(
                kind="upstream-pr",
                repo=repo,
                component=component,
                state=state,
                risk="high" if state == "blocked" else "medium",
                requires_approval=True,
                source="upstream-monitor",
                target_sha="",
                provenance="operator-action",
                evidence={
                    "current": row.get("current", ""),
                    "latest": row.get("latest", ""),
                    "safety": row.get("safety", ""),
                    "next_action": row.get("next_action", ""),
                },
                next_command_args=_upstream_monitor_command(repo),
                workflow_dispatch={
                    "workflow": "control-plane.yml",
                    "inputs": {
                        "mode": "upstream-monitor",
                        "repo": repo,
                        "dry_run": "true",
                    },
                },
            )
        )
    return actions


def _catalog_actions(destination_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in destination_rows:
        queue = row.get("sync_queue")
        if not isinstance(queue, list) or not queue:
            continue
        repo = _safe_name(row.get("repo", ""))
        if repo is None:
            continue
        actions.append(
            _action(
                kind="catalog-sync",
                repo=repo,
                component="catalog",
                state="queued",
                risk="low",
                requires_approval=False,
                source="catalog-readiness",
                target_sha="",
                provenance="operator-action",
                evidence={"sync_queue": queue},
                next_command_args=_catalog_sync_command(repo),
                workflow_dispatch={},
            )
        )
    return actions


def _standards_actions(cleanup_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in cleanup_rows:
        if row.get("state") != "drift" or row.get("provenance") == "local-only":
            continue
        repo = _safe_name(row.get("repo", ""))
        if repo is None:
            continue
        actions.append(
            _action(
                kind="drift-repair",
                repo=repo,
                component="standards",
                state="queued",
                risk="low",
                requires_approval=True,
                source="standards-reconcile",
                target_sha="",
                provenance="remote-confirmed",
                evidence={"findings": row.get("findings", [])},
                next_command_args=_standards_reconcile_command(repo),
                workflow_dispatch={
                    "workflow": "control-plane.yml",
                    "inputs": {
                        "mode": "standards-reconcile",
                        "repo": repo,
                        "dry_run": "true",
                    },
                },
            )
        )
    return actions


def _failure_retry_actions(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for failure in failures:
        run_id = _safe_run_id(failure.get("run_id", ""))
        if run_id is None:
            continue
        repo = _safe_repo_label(failure.get("repo", ""))
        component = _safe_name(failure.get("component", "") or "workflow")
        if repo is None or component is None:
            continue
        actions.append(
            _action(
                kind="failed-run-retry",
                repo=repo,
                component=component,
                state="queued",
                risk="medium",
                requires_approval=False,
                source="failure-classifier",
                target_sha=_safe_sha(failure.get("sha", "")),
                provenance="operator-action",
                evidence={
                    "root_cause": failure.get("root_cause", ""),
                    "confidence": failure.get("confidence", ""),
                    "next_action": failure.get("next_action", ""),
                },
                next_command_args=["gh", "run", "rerun", run_id],
                workflow_dispatch={},
            )
        )
    return actions


def _action(
    *,
    kind: str,
    repo: str,
    component: str,
    state: str,
    risk: str,
    requires_approval: bool,
    source: str,
    target_sha: str,
    provenance: str,
    evidence: dict[str, Any],
    next_command_args: list[str],
    workflow_dispatch: dict[str, Any],
) -> dict[str, Any]:
    next_command = shlex.join(next_command_args) if next_command_args else ""
    action = {
        "id": _action_id(kind, repo, component, target_sha or next_command),
        "repo": repo,
        "component": component,
        "kind": kind,
        "state": state,
        "risk": risk,
        "requires_approval": requires_approval,
        "source": source,
        "provenance": provenance,
        "evidence": evidence,
        "target_sha": target_sha,
        "next_command": next_command,
        "workflow_dispatch": workflow_dispatch,
    }
    safe = public_text_safe_value(action)
    assert_public_text(json.dumps(safe, sort_keys=True), context="queue action")
    return safe


def _registry_publish_dispatch(
    repo: str, *, component: str, sha: str
) -> dict[str, Any]:
    if not repo or not component or not _full_sha(sha):
        return {}
    return {
        "workflow": "control-plane.yml",
        "inputs": {
            "mode": "control-check",
            "repo": repo,
            "sha": sha,
            "event": "push",
            "publish": "true",
            "publish_component": component,
            "dry_run": "true",
        },
    }


def _gh_workflow_command(
    dispatch: dict[str, Any], *, force_dry_run: bool = False
) -> str:
    workflow = str(dispatch.get("workflow", "control-plane.yml"))
    if workflow not in ALLOWED_WORKFLOWS:
        return ""
    inputs = dispatch.get("inputs")
    inputs = inputs if isinstance(inputs, dict) else {}
    if force_dry_run:
        inputs = {**inputs, "dry_run": "true"}
    parts = ["gh", "workflow", "run", workflow]
    for key, value in sorted(inputs.items()):
        if key not in ALLOWED_WORKFLOW_INPUTS or value == "":
            return ""
        value_text = str(value)
        if not _safe_dispatch_value(value_text):
            return ""
        parts.extend(["-f", f"{key}={value_text}"])
    return " ".join(shlex.quote(part) for part in parts)


def _release_transaction_command(repo: str, component: str, sha: str) -> list[str]:
    if not sha:
        return []
    return fleet_command_args(
        "release",
        "transaction",
        "--repo",
        repo,
        "--component",
        component,
        "--sha",
        sha,
        "--dry-run",
    )


def _catalog_sync_command(repo: str) -> list[str]:
    return fleet_command_args(
        "sync-catalog",
        "--repo",
        repo,
        "--catalog-path",
        "../awesome-unraid",
        "--dry-run",
    )


def _upstream_monitor_command(repo: str) -> list[str]:
    return fleet_command_args(
        "upstream",
        "monitor",
        "--repo",
        repo,
        "--dry-run",
    )


def _standards_reconcile_command(repo: str) -> list[str]:
    return fleet_command_args(
        "standards",
        "reconcile",
        "--repo",
        repo,
        "--dry-run",
        "--format",
        "json",
    )


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("id", ""))
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        deduped.append(action)
    return sorted(
        deduped,
        key=lambda action: (
            str(action.get("risk", "")),
            str(action.get("kind", "")),
            str(action.get("repo", "")),
            str(action.get("component", "")),
        ),
    )


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _action_id(kind: str, repo: str, component: str, seed: str) -> str:
    base = ":".join([kind, repo or "fleet", component or "all"])
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    return _slug(f"{base}:{digest}")


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.:-]+", "-", value)
    return value.strip("-") or "action"


def _full_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", value or ""))


def _safe_dispatch_value(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.:/@-]{1,200}", value))


def _safe_name(value: Any) -> str | None:
    text = str(value or "")
    return text if SAFE_NAME_RE.fullmatch(text) else None


def _safe_repo_label(value: Any) -> str | None:
    text = str(value or "")
    return text if SAFE_REPO_LABEL_RE.fullmatch(text) else None


def _safe_run_id(value: Any) -> str | None:
    text = str(value or "")
    return text if SAFE_RUN_ID_RE.fullmatch(text) else None


def _safe_sha(value: Any) -> str:
    text = str(value or "")
    return text if _full_sha(text) else ""
