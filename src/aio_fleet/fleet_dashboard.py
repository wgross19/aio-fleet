from __future__ import annotations

import base64
import json
import os
import re
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aio_fleet.catalog import sync_catalog_assets
from aio_fleet.checks import CHECK_NAME
from aio_fleet.cleanup import cleanup_findings
from aio_fleet.command_text import fleet_command
from aio_fleet.failure_classifier import classify_workflow_state
from aio_fleet.fleet_queue import enrich_command_center_state
from aio_fleet.github_cli import github_cli_env
from aio_fleet.manifest import FleetManifest, RepoConfig
from aio_fleet.public_text import assert_public_text
from aio_fleet.registry import (
    compute_registry_tags,
    registry_failures_are_sha_only,
    registry_sha_tag_required,
    verify_registry_tags,
)
from aio_fleet.release_plan import release_plan_for_manifest
from aio_fleet.report import FleetReport, public_fleet_report_state, stable_report_json
from aio_fleet.safety import assess_upstream_pr
from aio_fleet.upstream import UpstreamMonitorResult, monitor_repo, upstream_branch
from aio_fleet.validators import catalog_repo_failures
from aio_fleet.workflow_health import control_plane_health

DASHBOARD_LABEL = "fleet-dashboard"
DASHBOARD_TITLE = "Fleet Command Center"
STATE_START = "<!-- aio-fleet-dashboard-state"
STATE_START_BASE64 = "<!-- aio-fleet-dashboard-state:base64"
STATE_END = "-->"
DASHBOARD_COMMANDS = {
    "rescan": "Rescan dashboard",
    "upstream_monitor": "Run upstream monitor",
    "standards_reconcile": "Run standards reconcile",
    "queue_publish_checks": "Queue publish checks",
}
GITHUB_CLI_TOKEN_KEYS = (
    "AIO_FLEET_DASHBOARD_TOKEN",
    "AIO_FLEET_UPSTREAM_TOKEN",
    "AIO_FLEET_ISSUE_TOKEN",
    "AIO_FLEET_CHECK_TOKEN",
    "APP_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
CHECKED_COMMAND_RE = re.compile(
    r"^-\s+\[[xX]\]\s+(?P<label>.+?)\s*$",
    re.MULTILINE,
)
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
GITHUB_ISSUE_BODY_SOFT_LIMIT = 60000
COMPACT_STATE_STRING_LIMIT = 500
COMPACT_STATE_LIST_LIMIT = 25


@dataclass(frozen=True)
class DashboardIssueResult:
    action: str
    number: int | None
    url: str


@dataclass(frozen=True)
class DashboardRepoRef:
    name: str
    github_repo: str
    path: Path
    raw: dict[str, Any]


def dashboard_report(
    manifest: FleetManifest,
    *,
    include_registry: bool = False,
    include_activity: bool = True,
    stale_days: int = 7,
    issue_repo: str = "wgross19/aio-fleet",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    warnings = alert_warnings(env, issue_repo=issue_repo)
    active_rows: list[dict[str, Any]] = []
    activity_rows: list[dict[str, Any]] = []
    registry_rows: list[dict[str, Any]] = []

    for repo in manifest.repos.values():
        repo_is_public = _is_public_config(repo.raw)
        if include_activity:
            activity_rows.append(
                _repo_activity_for_config(
                    repo.name, repo.github_repo, repo.raw, stale_days
                )
            )
        if repo.publish_profile == "template":
            active_rows.append(_template_row(repo, include_registry=include_registry))
            continue
        if not repo_is_public:
            active_rows.append(
                _private_active_row(repo.name, include_registry=include_registry)
            )
            continue
        repo_registry: dict[str, dict[str, Any]] = {}
        if include_registry:
            repo_registry = _repo_registry_states(repo)
            registry_rows.extend(repo_registry.values())
        try:
            results = monitor_repo(repo, write=False)
        except Exception as exc:
            active_rows.append(_monitor_failure_row(repo, exc))
            continue
        for result in results:
            active_rows.append(
                _dashboard_row(
                    repo,
                    result,
                    include_registry=include_registry,
                    registry_state=repo_registry.get(result.component),
                )
            )

    destination_rows = [
        _destination_row(
            manifest,
            ref,
            active_rows=active_rows,
            include_activity=include_activity,
            stale_days=stale_days,
        )
        for ref in _dashboard_repo_refs(manifest, "destination_repos")
    ]
    rehab_rows = [
        _rehab_row(ref, include_activity=include_activity, stale_days=stale_days)
        for ref in _dashboard_repo_refs(manifest, "rehab_repos")
    ]
    cleanup_rows = _cleanup_rows(manifest)
    release_rows = release_plan_for_manifest(
        manifest,
        include_registry=include_registry,
        catalog_sync=_catalog_sync_map(manifest),
        redact_private=True,
        registry_verify_attempts=1,
    )
    _apply_release_states(active_rows, release_rows, registry_rows)
    workflow = control_plane_health(repo=issue_repo)
    summary = dashboard_summary(
        active_rows=active_rows,
        activity_rows=activity_rows,
        destination_rows=destination_rows,
        rehab_rows=rehab_rows,
        registry_rows=registry_rows,
        release_rows=release_rows,
        cleanup_rows=cleanup_rows,
        workflow=workflow,
        warnings=warnings,
    )
    failures = classify_workflow_state(workflow)
    state = FleetReport(
        generated_at=generated_at,
        issue_repo=issue_repo,
        warnings=warnings,
        summary=summary,
        rows=active_rows,
        failures=failures,
        activity=activity_rows,
        destination_repos=destination_rows,
        rehab_repos=rehab_rows,
        registry=registry_rows,
        releases=release_rows,
        cleanup=cleanup_rows,
        workflow=workflow,
    ).to_state()
    state = _redact_private_dashboard_state(manifest, state)
    state = _with_refreshed_dashboard_summary(state)
    state = enrich_command_center_state(state)
    state = public_fleet_report_state(state)
    return {"state": state, "body": render_dashboard(state)}


def dashboard_summary(
    *,
    active_rows: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
    destination_rows: list[dict[str, Any]],
    rehab_rows: list[dict[str, Any]],
    registry_rows: list[dict[str, Any]],
    release_rows: list[dict[str, Any]],
    cleanup_rows: list[dict[str, Any]],
    workflow: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    repo_names = {str(row.get("repo", "")) for row in active_rows if row.get("repo")}
    update_rows = [row for row in active_rows if row.get("update")]
    registry_failures = sum(
        len(row.get("failures", []))
        for row in registry_rows
        if not registry_failures_are_sha_only(
            [str(failure) for failure in row.get("failures", [])]
        )
    )
    sha_tag_missing = len(
        [
            row
            for row in registry_rows
            if registry_failures_are_sha_only(
                [str(failure) for failure in row.get("failures", [])]
            )
        ]
    )
    release_due = len(
        [
            row
            for row in release_rows
            if row.get("state") in {"release-due", "catalog-sync-needed"}
        ]
    )
    publish_missing = len(
        [row for row in release_rows if row.get("state") == "publish-missing"]
    )
    cleanup_findings_total = sum(
        _int(row.get("findings_count")) for row in cleanup_rows
    )
    local_hygiene_total = sum(
        _int(row.get("local_findings_count")) for row in cleanup_rows
    )
    stale_prs = sum(_int(row.get("stale_prs")) for row in activity_rows) + sum(
        _int(row.get("stale_prs")) for row in destination_rows + rehab_rows
    )
    summary = {
        "active_repos": len(repo_names),
        "destination_repos": len(destination_rows),
        "rehab_repos": len(rehab_rows),
        "upstream_updates": len(update_rows),
        "ready_updates": len([row for row in update_rows if _is_ready_update(row)]),
        "triage_updates": len([row for row in update_rows if _is_triage_update(row)]),
        "blocked_updates": len([row for row in update_rows if _is_blocked_update(row)]),
        "runtime_deferred": len(
            [
                row
                for row in update_rows
                if row.get("runtime_smoke") == "deferred-to-main"
            ]
        ),
        "open_prs": sum(_int(row.get("open_prs")) for row in activity_rows)
        + sum(_int(row.get("open_prs")) for row in destination_rows)
        + sum(_int(row.get("open_prs")) for row in rehab_rows),
        "open_issues": sum(_int(row.get("open_issues")) for row in activity_rows)
        + sum(_int(row.get("open_issues")) for row in destination_rows)
        + sum(_int(row.get("open_issues")) for row in rehab_rows),
        "needs_response_issues": sum(
            _int(row.get("needs_response_issues")) for row in activity_rows
        )
        + sum(_int(row.get("needs_response_issues")) for row in destination_rows)
        + sum(_int(row.get("needs_response_issues")) for row in rehab_rows),
        "ready_prs": sum(_int(row.get("clean_prs")) for row in activity_rows)
        + sum(_int(row.get("clean_prs")) for row in destination_rows)
        + sum(_int(row.get("clean_prs")) for row in rehab_rows),
        "blocked_prs": sum(_int(row.get("blocked_prs")) for row in activity_rows)
        + sum(_int(row.get("blocked_prs")) for row in destination_rows)
        + sum(_int(row.get("blocked_prs")) for row in rehab_rows),
        "stale_prs": stale_prs,
        "registry_verified": len(registry_rows),
        "registry_failures": registry_failures,
        "sha_tag_missing": sha_tag_missing,
        "release_due": release_due,
        "publish_missing": publish_missing,
        "cleanup_findings": cleanup_findings_total,
        "local_hygiene": local_hygiene_total,
        "alert_warnings": len(warnings),
        "workflow_state": workflow.get("state", "unknown"),
    }
    summary["posture"] = _posture(summary)
    summary["remote_posture"] = _remote_posture(summary)
    summary["local_posture"] = "hygiene" if local_hygiene_total else "clean"
    return summary


def render_dashboard(state: dict[str, Any]) -> str:
    state = public_fleet_report_state(state)
    rows = list(state.get("rows", []))
    activity = list(state.get("activity", []))
    destination_rows = list(state.get("destination_repos", []))
    rehab_rows = list(state.get("rehab_repos", []))
    registry_rows = list(state.get("registry", []))
    release_rows = list(state.get("releases", []))
    cleanup_rows = list(state.get("cleanup", []))
    action_rows = list(state.get("actions", []))
    failure_rows = list(state.get("failures", []))
    approval_rows = list(state.get("approvals", []))
    catalog = dict(state.get("catalog", {}))
    standards = dict(state.get("standards", {}))
    candidates = dict(state.get("candidates", {}))
    workflow = dict(state.get("workflow", {}))
    warnings = list(state.get("warnings", []))
    summary = dict(state.get("summary", {}))
    ready = [row for row in rows if _is_ready_update(row)]
    triage = [row for row in rows if _is_triage_update(row)]
    blocked = [row for row in rows if _is_blocked_update(row)]
    lines = [
        "# Fleet Command Center",
        "",
        f"Last updated: `{state.get('generated_at', '')}`",
        "",
        "> Source repo updates start in each `<app>-aio` repo. `awesome-unraid` is the downstream catalog destination and sync follows validated source changes.",
        "",
    ]
    _render_command_center_section(
        lines,
        summary=summary,
        actions=action_rows,
        approvals=approval_rows,
        failures=failure_rows,
        catalog=catalog,
        standards=standards,
        candidates=candidates,
    )
    lines.extend(
        [
            "## Summary",
            "",
            (
                f"Posture: `{summary.get('posture', 'unknown')}` | "
                f"Remote: `{summary.get('remote_posture', 'unknown')}` | "
                f"Local: `{summary.get('local_posture', 'unknown')}`"
            ),
            "",
            "| Active | Destination | Rehab | Updates | Ready | Triage | Blocked | Registry Failures | SHA Tag Gaps | Release Due | Publish Missing | Cleanup Findings | Open PRs | Open Issues | Needs Response | Alert Warnings |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            "| {active_repos} | {destination_repos} | {rehab_repos} | {upstream_updates} | {ready_updates} | {triage_updates} | {blocked_updates} | {registry_failures} | {sha_tag_missing} | {release_due} | {publish_missing} | {cleanup_findings} | {open_prs} | {open_issues} | {needs_response_issues} | {alert_warnings} |".format(
                **{key: _cell(summary.get(key, 0)) for key in _summary_keys()}
            ),
            "",
        ]
    )
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    _render_safety_review_section(lines, rows)
    _render_update_section(lines, "Ready To Merge", ready)
    _render_update_section(lines, "Needs Triage", triage)
    _render_update_section(lines, "Blocked", blocked)
    _render_destination_section(lines, destination_rows)
    _render_rehab_section(lines, rehab_rows)
    _render_activity_section(lines, activity, destination_rows, rehab_rows)
    _render_workflow_section(lines, workflow)
    _render_registry_section(lines, registry_rows)
    _render_release_section(lines, release_rows)
    _render_cleanup_section(lines, cleanup_rows)
    _render_fleet_state_section(lines, rows)
    _render_controls(lines)
    _render_next_commands(lines, rows, destination_rows, rehab_rows)
    assert_public_text(stable_report_json(state), context="Fleet Command Center state")
    body = _dashboard_body_within_limit(lines, state)
    assert_public_text(body, context="Fleet Command Center body")
    return body


def _dashboard_body_within_limit(
    lines: list[str],
    state: dict[str, Any],
    *,
    limit: int = GITHUB_ISSUE_BODY_SOFT_LIMIT,
) -> str:
    body = _dashboard_body(lines, state)
    if _body_within_limit(body, limit):
        return body

    compact_state = _compact_dashboard_issue_state(state)
    compact_hidden_body = _dashboard_body(
        [
            *lines,
            "",
            (
                "> Detailed fleet state was compacted because the full dashboard "
                "exceeded GitHub's issue body limit. Use the workflow artifact for "
                "the complete JSON report."
            ),
        ],
        compact_state,
    )
    if _body_within_limit(compact_hidden_body, limit):
        return compact_hidden_body

    compact_body = _dashboard_body(
        _compact_dashboard_lines(state),
        _minimal_dashboard_issue_state(state),
    )
    if _body_within_limit(compact_body, limit):
        return compact_body

    emergency_body = _dashboard_body(
        _emergency_dashboard_lines(state),
        _emergency_dashboard_issue_state(state),
    )
    if _body_within_limit(emergency_body, limit):
        return emergency_body
    raise RuntimeError(
        "Fleet Command Center body still exceeds GitHub issue body budget after "
        "compaction"
    )


def _body_within_limit(body: str, limit: int) -> bool:
    return len(body) <= limit and len(body.encode("utf-8")) <= limit


def _dashboard_body(lines: list[str], hidden_state: dict[str, Any]) -> str:
    return "\n".join(
        [
            *lines,
            "",
            STATE_START_BASE64,
            _encoded_dashboard_state(hidden_state),
            STATE_END,
            "",
        ]
    )


def _compact_dashboard_lines(state: dict[str, Any]) -> list[str]:
    summary = dict(state.get("summary", {}))
    warnings = list(state.get("warnings", []))
    actions = list(state.get("actions", []))
    failures = list(state.get("failures", []))
    approvals = list(state.get("approvals", []))
    catalog = dict(state.get("catalog", {}))
    standards = dict(state.get("standards", {}))
    candidates = dict(state.get("candidates", {}))
    lines = [
        "# Fleet Command Center",
        "",
        f"Last updated: `{state.get('generated_at', '')}`",
        "",
        (
            "> Detailed fleet tables were compacted because the full dashboard "
            "exceeded GitHub's issue body limit. Use the workflow artifact for the "
            "complete JSON report."
        ),
        "",
    ]
    _render_command_center_section(
        lines,
        summary=summary,
        actions=actions[:10],
        approvals=approvals[:10],
        failures=failures[:10],
        catalog=catalog,
        standards=standards,
        candidates=candidates,
    )
    lines.extend(
        [
            "## Summary",
            "",
            (
                f"Posture: `{summary.get('posture', 'unknown')}` | "
                f"Remote: `{summary.get('remote_posture', 'unknown')}` | "
                f"Local: `{summary.get('local_posture', 'unknown')}`"
            ),
            "",
            "| Active | Destination | Rehab | Updates | Ready | Triage | Blocked | Registry Failures | SHA Tag Gaps | Release Due | Publish Missing | Cleanup Findings | Open PRs | Open Issues | Needs Response | Alert Warnings |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            "| {active_repos} | {destination_repos} | {rehab_repos} | {upstream_updates} | {ready_updates} | {triage_updates} | {blocked_updates} | {registry_failures} | {sha_tag_missing} | {release_due} | {publish_missing} | {cleanup_findings} | {open_prs} | {open_issues} | {needs_response_issues} | {alert_warnings} |".format(
                **{key: _cell(summary.get(key, 0)) for key in _summary_keys()}
            ),
            "",
        ]
    )
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {_cell(warning)}" for warning in warnings[:10])
        lines.append("")
    _render_controls(lines)
    return lines


def _emergency_dashboard_lines(state: dict[str, Any]) -> list[str]:
    summary = dict(state.get("summary", {}))
    warnings = list(state.get("warnings", []))
    lines = [
        "# Fleet Command Center",
        "",
        f"Last updated: `{state.get('generated_at', '')}`",
        "",
        (
            "> Dashboard detail was compacted because the generated fleet state "
            "exceeded GitHub's issue body limit. Use the workflow artifact for "
            "the complete JSON report."
        ),
        "",
        "## Summary",
        "",
        (
            f"Posture: `{summary.get('posture', 'unknown')}` | "
            f"Remote: `{summary.get('remote_posture', 'unknown')}` | "
            f"Local: `{summary.get('local_posture', 'unknown')}`"
        ),
        "",
        "| Active | Destination | Rehab | Updates | Ready | Triage | Blocked | Registry Failures | SHA Tag Gaps | Release Due | Publish Missing | Cleanup Findings | Open PRs | Open Issues | Needs Response | Alert Warnings |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| {active_repos} | {destination_repos} | {rehab_repos} | {upstream_updates} | {ready_updates} | {triage_updates} | {blocked_updates} | {registry_failures} | {sha_tag_missing} | {release_due} | {publish_missing} | {cleanup_findings} | {open_prs} | {open_issues} | {needs_response_issues} | {alert_warnings} |".format(
            **{key: _cell(summary.get(key, 0)) for key in _summary_keys()}
        ),
        "",
    ]
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {_cell(warning)}" for warning in warnings[:5])
        lines.append("")
    _render_controls(lines)
    return lines


def _encoded_dashboard_state(state: dict[str, Any]) -> str:
    raw = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _compact_dashboard_issue_state(state: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, list):
            compact[key] = [
                _compact_dashboard_value(item)
                for item in value[:COMPACT_STATE_LIST_LIMIT]
            ]
        else:
            compact[key] = _compact_dashboard_value(value)
    return public_fleet_report_state(compact)


def _minimal_dashboard_issue_state(state: dict[str, Any]) -> dict[str, Any]:
    minimal = {
        "schema_version": state.get("schema_version"),
        "generated_at": state.get("generated_at", ""),
        "issue_repo": state.get("issue_repo", ""),
        "warnings": list(state.get("warnings", []))[:10],
        "summary": dict(state.get("summary", {})),
        "rows": [],
        "actions": [
            _compact_dashboard_value(item)
            for item in list(state.get("actions", []))[:10]
        ],
        "failures": [
            _compact_dashboard_value(item)
            for item in list(state.get("failures", []))[:10]
        ],
        "approvals": [
            _compact_dashboard_value(item)
            for item in list(state.get("approvals", []))[:10]
        ],
        "catalog": _compact_dashboard_value(dict(state.get("catalog", {}))),
        "standards": _compact_dashboard_value(dict(state.get("standards", {}))),
        "candidates": _compact_dashboard_value(dict(state.get("candidates", {}))),
        "activity": [],
        "destination_repos": [],
        "rehab_repos": [],
        "registry": [],
        "releases": [],
        "cleanup": [],
        "workflow": _compact_dashboard_value(dict(state.get("workflow", {}))),
    }
    return public_fleet_report_state(minimal)


def _emergency_dashboard_issue_state(state: dict[str, Any]) -> dict[str, Any]:
    emergency = {
        "schema_version": state.get("schema_version"),
        "generated_at": state.get("generated_at", ""),
        "issue_repo": state.get("issue_repo", ""),
        "warnings": [
            _compact_dashboard_value(warning)
            for warning in list(state.get("warnings", []))[:5]
        ],
        "summary": dict(state.get("summary", {})),
        "rows": [],
        "actions": [],
        "failures": [],
        "approvals": [],
        "catalog": {},
        "standards": {},
        "candidates": {},
        "activity": [],
        "destination_repos": [],
        "rehab_repos": [],
        "registry": [],
        "releases": [],
        "cleanup": [],
        "workflow": {},
    }
    return public_fleet_report_state(emergency)


def _compact_dashboard_value(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        if len(value) <= COMPACT_STATE_STRING_LIMIT:
            return value
        return f"{value[: COMPACT_STATE_STRING_LIMIT - 3]}..."
    if isinstance(value, list):
        if depth >= 4:
            return []
        return [
            _compact_dashboard_value(item, depth=depth + 1)
            for item in value[:COMPACT_STATE_LIST_LIMIT]
        ]
    if isinstance(value, dict):
        if depth >= 4:
            return {}
        return {
            str(key): _compact_dashboard_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    return value


def _render_command_center_section(
    lines: list[str],
    *,
    summary: dict[str, Any],
    actions: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    catalog: dict[str, Any],
    standards: dict[str, Any],
    candidates: dict[str, Any],
) -> None:
    blockers = [
        action
        for action in actions
        if action.get("risk") == "high" or action.get("state") == "blocked"
    ]
    lines.extend(
        [
            "## Fleet Command Center",
            "",
            (
                f"Posture: `{summary.get('posture', 'unknown')}` | "
                f"Remote: `{summary.get('remote_posture', 'unknown')}` | "
                f"Local: `{summary.get('local_posture', 'unknown')}`"
            ),
            "",
            "| Actions | Blockers | Pending Approvals | Failures | Catalog | Standards |",
            "| ---: | ---: | ---: | ---: | --- | --- |",
            "| {actions} | {blockers} | {approvals} | {failures} | {catalog} | {standards} |".format(
                actions=_cell(summary.get("actions_queued", len(actions))),
                blockers=_cell(len(blockers)),
                approvals=_cell(summary.get("pending_approvals", len(approvals))),
                failures=_cell(summary.get("failure_classifications", len(failures))),
                catalog=_cell(catalog.get("state", "unknown")),
                standards=_cell(standards.get("state", "unknown")),
            ),
            "",
        ]
    )
    _render_action_table(lines, "Current Blockers", blockers)
    _render_approval_table(lines, approvals)
    _render_action_table(lines, "Release Queue", _actions_by_kind(actions, "release"))
    _render_action_table(lines, "Upstream Queue", _actions_by_kind(actions, "upstream"))
    _render_action_table(lines, "Catalog Queue", _actions_by_kind(actions, "catalog"))
    _render_failure_table(lines, failures)
    _render_candidate_lane(lines, candidates)


def _render_action_table(
    lines: list[str],
    title: str,
    actions: list[dict[str, Any]],
) -> None:
    lines.extend([f"### {title}", ""])
    if not actions:
        lines.extend(["No queued items.", ""])
        return
    lines.extend(
        [
            "| ID | Kind | Repo | Component | State | Risk | Next Action |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for action in actions[:10]:
        lines.append(
            "| {id} | {kind} | {repo} | {component} | {state} | {risk} | {next_action} |".format(
                id=f"`{_cell(action.get('id', ''))}`",
                kind=_cell(action.get("kind", "")),
                repo=_cell(action.get("repo", "")),
                component=_cell(action.get("component", "")),
                state=_cell(action.get("state", "")),
                risk=_cell(action.get("risk", "")),
                next_action=_cell(action.get("next_command", "")),
            )
        )
    lines.append("")


def _render_approval_table(lines: list[str], approvals: list[dict[str, Any]]) -> None:
    lines.extend(["### Pending Approvals", ""])
    if not approvals:
        lines.extend(["No protected approvals are waiting in the queue.", ""])
        return
    lines.extend(
        [
            "| ID | Repo | Component | SHA | Risk | Next Action |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for approval in approvals[:10]:
        lines.append(
            "| {id} | {repo} | {component} | {sha} | {risk} | {next_action} |".format(
                id=f"`{_cell(approval.get('id', ''))}`",
                repo=_cell(approval.get("repo", "")),
                component=_cell(approval.get("component", "")),
                sha=_cell(_short_sha(str(approval.get("target_sha", "")))),
                risk=_cell(approval.get("risk", "")),
                next_action=_cell(approval.get("next_action", "")),
            )
        )
    lines.append("")


def _render_failure_table(lines: list[str], failures: list[dict[str, Any]]) -> None:
    lines.extend(["### Recent Failure Classifications", ""])
    if not failures:
        lines.extend(["No classified recent failures.", ""])
        return
    lines.extend(
        [
            "| Run | Root Cause | Confidence | Summary | Next Action |",
            "| --- | --- | ---: | --- | --- |",
        ]
    )
    for failure in failures[:10]:
        run_id = str(failure.get("run_id", ""))
        run_url = str(failure.get("run_url", ""))
        run = f"[{run_id}]({run_url})" if run_id and run_url else run_id
        lines.append(
            "| {run} | {root} | {confidence} | {summary} | {next_action} |".format(
                run=_cell(run),
                root=_cell(failure.get("root_cause", "")),
                confidence=_cell(failure.get("confidence", "")),
                summary=_cell(failure.get("summary", "")),
                next_action=_cell(failure.get("next_action", "")),
            )
        )
    lines.append("")


def _render_candidate_lane(lines: list[str], candidates: dict[str, Any]) -> None:
    required = candidates.get("required_bootstrap", [])
    required = required if isinstance(required, list) else []
    lines.extend(
        [
            "### New AIO Candidate Lane",
            "",
            f"State: `{_cell(candidates.get('state', 'planning'))}`",
            "",
        ]
    )
    if required:
        lines.append("Bootstrap gates:")
        lines.extend(f"- {_cell(item)}" for item in required[:8])
        lines.append("")


def _actions_by_kind(
    actions: list[dict[str, Any]], prefix: str
) -> list[dict[str, Any]]:
    return [
        action for action in actions if str(action.get("kind", "")).startswith(prefix)
    ]


def _short_sha(value: str) -> str:
    return value[:12] if FULL_SHA_RE.fullmatch(value) else value


def alert_warnings(
    env: dict[str, str], *, issue_repo: str = "wgross19/aio-fleet"
) -> list[str]:
    warnings: list[str] = []
    if not env.get("AIO_FLEET_ALERT_WEBHOOK_URL"):
        if not _github_actions_secret_exists(issue_repo, "AIO_FLEET_ALERT_WEBHOOK_URL"):
            warnings.append(
                "AIO_FLEET_ALERT_WEBHOOK_URL is not configured; rich digest alerts are disabled."
            )
    return warnings


def _github_actions_secret_exists(repo: str, name: str) -> bool:
    try:
        result = subprocess.run(  # nosec B603 B607
            ["gh", "secret", "list", "--repo", repo, "--json", "name"],
            check=False,
            text=True,
            capture_output=True,
            env=github_cli_env(
                (
                    "AIO_FLEET_DASHBOARD_TOKEN",
                    "AIO_FLEET_WORKFLOW_TOKEN",
                    "AIO_FLEET_CHECK_TOKEN",
                    "APP_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                )
            ),
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return any(isinstance(item, dict) and item.get("name") == name for item in payload)


def _is_public_config(raw: dict[str, Any]) -> bool:
    return raw.get("public") is True


def _private_repo_names(manifest: FleetManifest) -> set[str]:
    return {
        repo.name for repo in manifest.repos.values() if not _is_public_config(repo.raw)
    }


def _redact_private_dashboard_state(
    manifest: FleetManifest, state: dict[str, Any]
) -> dict[str, Any]:
    """Keep public dashboard state safe even if a new collector forgets privacy."""
    private_repos = _private_repo_names(manifest)
    if not private_repos:
        return state
    redacted = dict(state)
    redacted["rows"] = [
        (
            _redacted_active_row(row)
            if str(row.get("repo", "")) in private_repos
            else row
        )
        for row in list(state.get("rows", []))
        if isinstance(row, dict)
    ]
    redacted["registry"] = [
        row
        for row in list(state.get("registry", []))
        if isinstance(row, dict) and str(row.get("repo", "")) not in private_repos
    ]
    redacted["releases"] = [
        (
            _redacted_release_row(row)
            if str(row.get("repo", "")) in private_repos
            else row
        )
        for row in list(state.get("releases", []))
        if isinstance(row, dict)
    ]
    redacted["cleanup"] = [
        (
            _redacted_cleanup_row(str(row.get("repo", "")))
            if str(row.get("repo", "")) in private_repos
            else row
        )
        for row in list(state.get("cleanup", []))
        if isinstance(row, dict)
    ]
    for key in ("actions", "approvals", "failures"):
        redacted[key] = [
            row
            for row in list(state.get(key, []))
            if isinstance(row, dict) and str(row.get("repo", "")) not in private_repos
        ]
    return redacted


def _with_refreshed_dashboard_summary(state: dict[str, Any]) -> dict[str, Any]:
    refreshed = dict(state)
    refreshed["summary"] = dashboard_summary(
        active_rows=[
            row for row in list(refreshed.get("rows", [])) if isinstance(row, dict)
        ],
        activity_rows=[
            row for row in list(refreshed.get("activity", [])) if isinstance(row, dict)
        ],
        destination_rows=[
            row
            for row in list(refreshed.get("destination_repos", []))
            if isinstance(row, dict)
        ],
        rehab_rows=[
            row
            for row in list(refreshed.get("rehab_repos", []))
            if isinstance(row, dict)
        ],
        registry_rows=[
            row for row in list(refreshed.get("registry", [])) if isinstance(row, dict)
        ],
        release_rows=[
            row for row in list(refreshed.get("releases", [])) if isinstance(row, dict)
        ],
        cleanup_rows=[
            row for row in list(refreshed.get("cleanup", [])) if isinstance(row, dict)
        ],
        workflow=(
            dict(refreshed.get("workflow", {}))
            if isinstance(refreshed.get("workflow"), dict)
            else {}
        ),
        warnings=list(refreshed.get("warnings", [])),
    )
    return refreshed


def upsert_dashboard_issue(
    *,
    issue_repo: str,
    body: str,
    issue_number: int | None = None,
    title: str = DASHBOARD_TITLE,
    label: str = DASHBOARD_LABEL,
    dry_run: bool,
) -> DashboardIssueResult:
    assert_public_text(body, context="Fleet Command Center body")
    existing = (
        _dashboard_issue_by_number(issue_repo, issue_number)
        if issue_number
        else _find_dashboard_issue(issue_repo, label=label)
    )
    if dry_run:
        return DashboardIssueResult(
            action="would-update" if existing else "would-create",
            number=int(existing["number"]) if existing else None,
            url=str(existing.get("url", "")) if existing else "",
        )
    _ensure_label(issue_repo, label=label)
    if existing:
        number = int(existing["number"])
        _run_with_issue_body_file(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "--repo",
                issue_repo,
                "--title",
                title,
            ],
            body=body,
            cli_scope="issue",
        )
        _add_dashboard_label(issue_repo, number=number, label=label)
        _close_duplicate_dashboard_issues(
            issue_repo,
            canonical_number=number,
            title=title,
        )
        return DashboardIssueResult(
            action="updated",
            number=number,
            url=str(existing.get("url", "")),
        )
    created = _run_with_issue_body_file(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            issue_repo,
            "--title",
            title,
            "--label",
            label,
        ],
        body=body,
        cli_scope="issue",
    )
    url = created.stdout.strip()
    return DashboardIssueResult(
        action="created",
        number=_issue_number_from_url(url),
        url=url,
    )


def _run_with_issue_body_file(
    command: list[str],
    *,
    body: str,
    cli_scope: str,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [*command, "--body-file", "-"],
        cli_scope=cli_scope,
        input_text=body,
    )


def dashboard_commands_from_body(body: str) -> dict[str, bool]:
    checked = {
        match.group("label").strip() for match in CHECKED_COMMAND_RE.finditer(body)
    }
    return {name: label in checked for name, label in DASHBOARD_COMMANDS.items()}


def dashboard_issue_commands(*, issue_repo: str, issue_number: int) -> dict[str, Any]:
    issue = _gh_json(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            issue_repo,
            "--json",
            "number,title,state,body,labels,url",
        ],
        cli_scope="issue",
    )
    if not isinstance(issue, dict):
        return {
            "issue_number": issue_number,
            "issue_url": "",
            "is_dashboard": False,
            "commands": {},
        }
    labels = [
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    ]
    is_dashboard = (
        str(issue.get("title", "")) == DASHBOARD_TITLE
        and str(issue.get("state", "")).upper() == "OPEN"
        and DASHBOARD_LABEL in labels
    )
    commands = (
        dashboard_commands_from_body(str(issue.get("body", ""))) if is_dashboard else {}
    )
    return {
        "issue_number": issue_number,
        "issue_url": str(issue.get("url", "")),
        "is_dashboard": is_dashboard,
        "commands": commands,
        "requested": any(commands.values()),
    }


def repo_activity(name: str, github_repo: str, stale_days: int) -> dict[str, Any]:
    try:
        prs = _gh_json(
            [
                "pr",
                "list",
                "--repo",
                github_repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,url,isDraft,mergeStateStatus,statusCheckRollup,createdAt,headRefOid",
            ]
        )
        issues = _gh_json(
            [
                "issue",
                "list",
                "--repo",
                github_repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,url,createdAt,labels",
            ]
        )
    except Exception as exc:
        return {
            "repo": name,
            "github_repo": github_repo,
            "activity_state": "unknown",
            "error": str(exc),
            "open_prs": "unknown",
            "open_issues": "unknown",
            "draft_prs": "unknown",
            "blocked_prs": "unknown",
            "clean_prs": "unknown",
            "stale_prs": "unknown",
            "oldest_pr_age_days": "unknown",
            "oldest_issue_age_days": "unknown",
            "newest_issue_age_days": "unknown",
            "oldest_pr": {},
            "oldest_issue": {},
            "prs": [],
            "issues": [],
            "needs_response_issues": "unknown",
        }
    prs = prs if isinstance(prs, list) else []
    issues = issues if isinstance(issues, list) else []
    pr_rows = [
        _pr_summary(pr, stale_days=stale_days) for pr in prs if isinstance(pr, dict)
    ]
    issue_rows = [
        _issue_summary(issue, stale_days=stale_days)
        for issue in issues
        if isinstance(issue, dict)
    ]
    issue_ages = [
        _age_days(issue.get("createdAt")) for issue in issues if isinstance(issue, dict)
    ]
    pr_ages = [_age_days(pr.get("createdAt")) for pr in prs if isinstance(pr, dict)]
    oldest_pr = max(pr_rows, key=lambda pr: _int(pr.get("age_days")), default={})
    oldest_issue = max(
        issue_rows, key=lambda issue: _int(issue.get("age_days")), default={}
    )
    return {
        "repo": name,
        "github_repo": github_repo,
        "activity_state": "ok",
        "open_prs": len(prs),
        "open_issues": len(issues),
        "draft_prs": len([pr for pr in pr_rows if pr["state"] == "draft"]),
        "blocked_prs": len([pr for pr in pr_rows if pr["state"] == "blocked"]),
        "clean_prs": len([pr for pr in pr_rows if pr["state"] == "ready"]),
        "stale_prs": len([pr for pr in pr_rows if pr["stale"]]),
        "oldest_pr_age_days": max(pr_ages) if pr_ages else 0,
        "oldest_issue_age_days": max(issue_ages) if issue_ages else 0,
        "newest_issue_age_days": min(issue_ages) if issue_ages else 0,
        "oldest_pr": oldest_pr,
        "oldest_issue": oldest_issue,
        "prs": pr_rows,
        "issues": _dashboard_issue_rows(issue_rows),
        "needs_response_issues": len(
            [issue for issue in issue_rows if issue["needs_response"]]
        ),
    }


def _template_row(repo: RepoConfig, *, include_registry: bool) -> dict[str, Any]:
    return {
        "repo": repo.name,
        "component": "template",
        "current": "",
        "latest": "",
        "strategy": "manual",
        "update": False,
        "pr": "",
        "check": "not-applicable",
        "signed": "not-applicable",
        "registry": "manual" if include_registry else "not-run",
        "release": "manual",
        "safety": "not-applicable",
        "safety_confidence": "",
        "config_delta": "",
        "template_impact": "",
        "runtime_smoke": "",
        "safety_signals": [],
        "safety_warnings": [],
        "safety_failures": [],
        "next_action": "manual template baseline",
    }


def _private_active_row(repo: str, *, include_registry: bool) -> dict[str, Any]:
    return {
        "repo": repo,
        "component": "private",
        "current": "private",
        "latest": "private",
        "strategy": "private",
        "update": False,
        "pr": "",
        "check": "private-skipped",
        "signed": "private-skipped",
        "registry": "private-skipped" if include_registry else "not-run",
        "release": "private-skipped",
        "safety": "private-skipped",
        "safety_confidence": "",
        "config_delta": "private",
        "template_impact": "private",
        "runtime_smoke": "private",
        "safety_signals": [],
        "safety_warnings": [],
        "safety_failures": [],
        "next_action": "private repo details redacted",
    }


def _redacted_active_row(row: dict[str, Any]) -> dict[str, Any]:
    repo = str(row.get("repo", ""))
    registry = str(row.get("registry", "not-run"))
    redacted = _private_active_row(
        repo, include_registry=registry not in {"", "not-run"}
    )
    redacted["registry"] = "not-run" if registry == "not-run" else "private-skipped"
    return redacted


def _redacted_release_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": str(row.get("repo", "")),
        "component": "private",
        "profile": "private-skipped",
        "sha": "",
        "latest_release_tag": "private-skipped",
        "latest_changelog_version": "private-skipped",
        "latest_github_release": {"state": "private-skipped"},
        "next_version": "",
        "release_due": False,
        "catalog_sync_needed": False,
        "registry_state": "private-skipped",
        "registry_tags": {"dockerhub": [], "ghcr": []},
        "registry_failures": [],
        "state": "private-skipped",
        "blockers": [],
        "warnings": ["private-skipped"],
        "next_action": "private-skipped",
        "operator_commands": {},
    }


def _redacted_cleanup_row(repo: str) -> dict[str, Any]:
    return {
        "repo": repo,
        "state": "private-skipped",
        "provenance": "private-skipped",
        "findings_count": 0,
        "findings": [],
        "local_findings_count": 0,
        "local_findings": [],
    }


def _monitor_failure_row(repo: RepoConfig, exc: Exception) -> dict[str, Any]:
    return {
        "repo": repo.name,
        "component": "aio",
        "current": "",
        "latest": "",
        "strategy": "unknown",
        "update": False,
        "pr": "",
        "check": "unknown",
        "signed": "unknown",
        "registry": "not-run",
        "release": "unknown",
        "safety": "unknown",
        "safety_confidence": "",
        "config_delta": "unknown",
        "template_impact": "unknown",
        "runtime_smoke": "unknown",
        "safety_signals": [],
        "safety_warnings": [str(exc)],
        "safety_failures": [],
        "next_action": f"upstream monitor failed: {exc}",
    }


def _dashboard_row(
    repo: RepoConfig,
    result: UpstreamMonitorResult,
    *,
    include_registry: bool,
    registry_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if getattr(result, "blocked", False):
        reason = str(getattr(result, "blocked_reason", "upstream update blocked"))
        next_action = str(getattr(result, "next_action", "resolve upstream blocker"))
        return {
            "repo": repo.name,
            "component": result.component,
            "current": result.current_version,
            "latest": result.latest_version,
            "strategy": result.strategy,
            "update": result.updates_available,
            "pr": "",
            "check": "blocked",
            "signed": "blocked",
            "registry": "not-run",
            "registry_detail": {},
            "release": "blocked",
            "safety": "blocked",
            "safety_confidence": "",
            "config_delta": "submodule-ref-missing",
            "template_impact": "none",
            "runtime_smoke": "blocked",
            "safety_signals": [],
            "safety_warnings": [reason],
            "safety_failures": [reason],
            "next_action": next_action,
        }
    branch = upstream_branch(repo, [result]) if result.strategy == "pr" else ""
    pr = _open_pr(repo, branch) if branch else None
    pr_url = str(pr.get("url", "")) if pr else ""
    pr_label = f"[#{pr['number']}]({pr_url})" if pr and pr_url else ""
    check = (
        _check_state(pr)
        if pr
        else ("not-needed" if not result.updates_available else "missing")
    )
    signed = (
        _signed_state(repo, pr)
        if pr
        else ("not-needed" if not result.updates_available else "missing")
    )
    safety = _safety_assessment(repo, result, pr, signed=signed, check=check)
    registry = "not-run"
    if include_registry:
        registry = (
            _registry_label(registry_state)
            if result.component in _publish_components(repo)
            else "not-applicable"
        )
    release = "after-merge" if result.updates_available else "current"
    next_action = _next_action(
        result,
        pr=pr,
        signed=signed,
        check=check,
        safety=safety,
    )
    return {
        "repo": repo.name,
        "component": result.component,
        "current": result.current_version,
        "latest": result.latest_version,
        "strategy": result.strategy,
        "update": result.updates_available,
        "pr": pr_label,
        "check": check,
        "signed": signed,
        "registry": registry,
        "registry_detail": registry_state or {},
        "release": release,
        "safety": safety["safety_level"],
        "safety_confidence": safety["confidence"],
        "config_delta": safety["config_delta"],
        "template_impact": safety["template_impact"],
        "runtime_smoke": safety["runtime_smoke"],
        "safety_signals": safety.get("signals", []),
        "safety_warnings": safety["warnings"],
        "safety_failures": safety["failures"],
        "next_action": next_action,
    }


def _destination_row(
    manifest: FleetManifest,
    ref: DashboardRepoRef,
    *,
    active_rows: list[dict[str, Any]],
    include_activity: bool,
    stale_days: int,
) -> dict[str, Any]:
    activity = (
        _repo_activity_for_config(ref.name, ref.github_repo, ref.raw, stale_days)
        if include_activity
        else _empty_activity_for_config(ref.name, ref.github_repo, ref.raw)
    )
    if not _is_public_config(ref.raw):
        return {
            **activity,
            "kind": "destination",
            "role": str(ref.raw.get("role", "destination")),
            "description": "",
            "catalog_state": "private-skipped",
            "catalog_findings": [],
            "sync_queue": [],
            "sync_queue_count": 0,
            "next_action": "private repo details redacted",
        }
    catalog_path = Path(str(ref.raw.get("catalog_path") or ref.path))
    failures = catalog_repo_failures(manifest, catalog_path)
    sync_queue = [
        {
            "repo": row.get("repo", ""),
            "component": row.get("component", ""),
            "pr": row.get("pr", ""),
            "state": row.get("next_action", ""),
        }
        for row in active_rows
        if _is_ready_update(row)
    ]
    return {
        **activity,
        "kind": "destination",
        "role": str(ref.raw.get("role", "destination")),
        "description": str(ref.raw.get("description", "")),
        "catalog_state": "ok" if not failures else f"{len(failures)} finding(s)",
        "catalog_findings": failures[:10],
        "sync_queue": sync_queue,
        "sync_queue_count": len(sync_queue),
        "next_action": str(ref.raw.get("next_action", "validate destination repo")),
    }


def _rehab_row(
    ref: DashboardRepoRef,
    *,
    include_activity: bool,
    stale_days: int,
) -> dict[str, Any]:
    activity = (
        _repo_activity_for_config(ref.name, ref.github_repo, ref.raw, stale_days)
        if include_activity
        else _empty_activity_for_config(ref.name, ref.github_repo, ref.raw)
    )
    if not _is_public_config(ref.raw):
        return {
            **activity,
            "kind": "rehab",
            "status": str(ref.raw.get("status", "rehab")),
            "description": "",
            "branch": "private",
            "dirty": "private",
            "path_exists": "private",
            "cleanup_findings": 0,
            "cleanup_paths": [],
            "next_action": "private repo details redacted",
            "checklist": [],
        }
    repo_config = RepoConfig(
        name=ref.name,
        raw={
            "path": str(ref.path),
            "app_slug": ref.name,
            "image_name": f"wgross19/{ref.name}",
            "docker_cache_scope": f"{ref.name}-image",
            "pytest_image_tag": f"{ref.name}:pytest",
        },
        defaults={},
        owner=ref.github_repo.split("/", 1)[0],
    )
    findings = cleanup_findings(repo_config) if ref.path.exists() else []
    git_state = _git_state(ref.path)
    return {
        **activity,
        "kind": "rehab",
        "status": str(ref.raw.get("status", "rehab")),
        "description": str(ref.raw.get("description", "")),
        "branch": git_state["branch"],
        "dirty": git_state["dirty"],
        "path_exists": git_state["path_exists"],
        "cleanup_findings": len(findings),
        "cleanup_paths": [finding.path.name for finding in findings[:10]],
        "next_action": str(ref.raw.get("next_action", "run rehab onboarding")),
        "checklist": rehab_checklist(ref.name),
    }


def rehab_checklist(repo: str) -> list[str]:
    return [
        "sync local repo to main",
        "inspect Dockerfile, runtime wrapper, XML, README, and support docs",
        "decide publish profile and upstream monitor strategy",
        "export .aio-fleet.yml from fleet.yml once manifest entry is ready",
        "remove legacy workflows/config/scripts that aio-fleet replaces",
        "run central validation and cleanup verification",
        "prove aio-fleet / required on a real PR",
        f"promote {repo} to active fleet only after validation passes",
    ]


def _repo_registry_states(repo: RepoConfig) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    sha = _git_head(repo.path)
    components = _publish_components(repo)
    for component in components:
        try:
            tags = compute_registry_tags(
                repo,
                sha=sha,
                component=component,
                include_sha_tag=registry_sha_tag_required(
                    repo, sha=sha, component=component
                ),
            )
            failures = verify_registry_tags(tags.all_tags, dockerhub_attempts=1)
            states[component] = {
                "repo": repo.name,
                "component": component,
                "sha": sha,
                "dockerhub": tags.dockerhub,
                "ghcr": tags.ghcr,
                "failures": failures,
                "state": _registry_row_state(failures),
                "verified_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
        except Exception as exc:
            states[component] = {
                "repo": repo.name,
                "component": component,
                "sha": sha,
                "dockerhub": [],
                "ghcr": [],
                "failures": [str(exc)],
                "state": "failed",
                "verified_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
    return states


def _registry_label(state: dict[str, Any] | None) -> str:
    if not state:
        return "unknown"
    failures = state.get("failures", [])
    if isinstance(failures, list) and failures:
        prefix = "sha-missing" if registry_failures_are_sha_only(failures) else "failed"
        return f"{prefix}:{len(failures)}"
    dockerhub = len(state.get("dockerhub", []))
    ghcr = len(state.get("ghcr", []))
    return f"ok:{dockerhub}+{ghcr} tags"


def _registry_row_state(failures: list[str]) -> str:
    if not failures:
        return "ok"
    if registry_failures_are_sha_only(failures):
        return "sha-tag-missing"
    return "failed"


def _publish_components(repo: RepoConfig) -> list[str]:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return ["aio"]
    names = [
        name
        for name, config in components.items()
        if name == "aio" or (isinstance(config, dict) and config.get("image_name"))
    ]
    return names or ["aio"]


def _cleanup_rows(manifest: FleetManifest) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for repo in manifest.repos.values():
        if not _is_public_config(repo.raw):
            rows.append(_redacted_cleanup_row(repo.name))
            continue
        all_findings = cleanup_findings(repo)
        findings = [
            finding for finding in all_findings if finding.provenance != "local-only"
        ]
        local_findings = [
            finding for finding in all_findings if finding.provenance == "local-only"
        ]
        rows.append(
            {
                "repo": repo.name,
                "state": (
                    "drift" if findings else "local-only" if local_findings else "ok"
                ),
                "provenance": (
                    "remote-confirmed"
                    if findings or not local_findings
                    else "local-only"
                ),
                "findings_count": len(findings),
                "findings": [
                    {
                        "path": str(finding.path.relative_to(repo.path)),
                        "reason": finding.reason,
                        "provenance": finding.provenance,
                    }
                    for finding in findings[:10]
                ],
                "local_findings_count": len(local_findings),
                "local_findings": [
                    {
                        "path": str(finding.path.relative_to(repo.path)),
                        "reason": finding.reason,
                        "provenance": finding.provenance,
                    }
                    for finding in local_findings[:10]
                ],
            }
        )
    return rows


def _catalog_sync_map(manifest: FleetManifest) -> dict[str, bool]:
    catalog_path = _awesome_unraid_catalog_path(manifest)
    if catalog_path is None or not catalog_path.exists():
        return {}
    result: dict[str, bool] = {}
    for repo in manifest.repos.values():
        if repo.raw.get("public") is not True or not repo.raw.get("catalog_assets"):
            continue
        try:
            changes = sync_catalog_assets(
                manifest,
                catalog_path=catalog_path,
                repos=[repo],
                icon_only=False,
                dry_run=True,
            )
        except (FileNotFoundError, ValueError):
            result[repo.name] = True
            continue
        if changes:
            result[repo.name] = True
    return result


def _awesome_unraid_catalog_path(manifest: FleetManifest) -> Path | None:
    dashboard = manifest.raw.get("dashboard")
    if isinstance(dashboard, dict):
        destinations = dashboard.get("destination_repos")
        if isinstance(destinations, dict):
            awesome = destinations.get("awesome-unraid")
            if isinstance(awesome, dict):
                raw_path = awesome.get("catalog_path") or awesome.get("path")
                if raw_path:
                    return Path(str(raw_path))
    return None


def _apply_release_states(
    active_rows: list[dict[str, Any]],
    release_rows: list[dict[str, Any]],
    registry_rows: list[dict[str, Any]],
) -> None:
    release_by_key = {
        (str(row.get("repo")), str(row.get("component", "aio"))): row
        for row in release_rows
    }
    registry_by_key = {
        (str(row.get("repo")), str(row.get("component", "aio"))): row
        for row in registry_rows
    }
    for row in active_rows:
        repo = str(row.get("repo", ""))
        component = str(row.get("component", "aio"))
        release = release_by_key.get((repo, component)) or release_by_key.get(
            (repo, "aio")
        )
        if release:
            row["release"] = release.get("state", row.get("release", "unknown"))
            if release.get("state") == "private-skipped":
                row.pop("release_detail", None)
            else:
                row["release_detail"] = release
        registry = registry_by_key.get((repo, component))
        if registry:
            row["registry"] = _registry_label(registry)
            if registry.get("state") == "private-skipped":
                row.pop("registry_detail", None)
            else:
                row["registry_detail"] = registry


def _next_action(
    result: UpstreamMonitorResult,
    *,
    pr: dict[str, Any] | None,
    signed: str,
    check: str,
    safety: dict[str, Any],
) -> str:
    if not result.updates_available:
        return "none"
    if result.strategy == "notify":
        return "manual triage; notify-only strategy"
    if not pr:
        return "open signed source repo PR"
    merge_state = str(pr.get("mergeStateStatus") or "").lower()
    if signed != "verified":
        return "regenerate/update PR with verified signed commit"
    if check != "success":
        return f"wait for central check: {check}"
    if merge_state in {"behind", "blocked", "dirty"}:
        return f"resolve PR state: {merge_state}"
    if safety.get("safety_level") == "blocked":
        return str(safety.get("next_action", "resolve safety failure before merge"))
    if safety.get("safety_level") == "warn":
        return str(safety.get("next_action", "review safety warnings before merge"))
    return "human review and merge"


def _safety_assessment(
    repo: RepoConfig,
    result: UpstreamMonitorResult,
    pr: dict[str, Any] | None,
    *,
    signed: str,
    check: str,
) -> dict[str, Any]:
    if not result.updates_available:
        return {
            "safety_level": "not-needed",
            "confidence": "",
            "config_delta": "none",
            "template_impact": "none",
            "runtime_smoke": "not-run",
            "signals": [],
            "warnings": [],
            "failures": [],
            "next_action": "none",
        }
    try:
        return assess_upstream_pr(
            repo,
            result=result,
            pr=pr,
            signed_state=signed,
            check_state=check,
        ).to_dict()
    except Exception as exc:
        return {
            "safety_level": "warn",
            "confidence": 0.25,
            "config_delta": "unknown",
            "template_impact": "unknown",
            "runtime_smoke": "unknown",
            "signals": [],
            "warnings": [f"safety assessment failed: {exc}"],
            "failures": [],
            "next_action": "review safety assessment failure before merge",
        }


def _open_pr(repo: RepoConfig, branch: str) -> dict[str, Any] | None:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--head",
            branch,
            "--base",
            "main",
            "--json",
            "number,url,files,headRefName,baseRefName,headRefOid,mergeStateStatus,statusCheckRollup",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data[0] if data else None


def _check_state(pr: dict[str, Any] | None) -> str:
    if not pr:
        return "missing"
    for check in pr.get("statusCheckRollup", []):
        if isinstance(check, dict) and check.get("name") == CHECK_NAME:
            if check.get("status") == "COMPLETED":
                return str(check.get("conclusion", "unknown")).lower()
            return str(check.get("status", "unknown")).lower()
    return "missing"


def _signed_state(repo: RepoConfig, pr: dict[str, Any] | None) -> str:
    if not pr:
        return "missing"
    number = str(pr.get("number") or "")
    if not number:
        return "missing"
    result = _run(
        [
            "gh",
            "api",
            f"repos/{repo.github_repo}/pulls/{number}/commits",
            "--paginate",
        ],
        check=False,
    )
    if result.returncode != 0:
        return "unknown"
    try:
        commits = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(commits, list) or not commits:
        return "missing"
    reasons: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        verification = commit.get("commit", {}).get("verification", {})
        if isinstance(verification, dict) and verification.get("verified") is True:
            continue
        reasons.append(str(verification.get("reason") or "unverified"))
    if not reasons:
        return "verified"
    return ",".join(sorted(set(reasons)))


def _find_dashboard_issue(issue_repo: str, *, label: str) -> dict[str, Any] | None:
    candidates = _dashboard_issue_candidates(issue_repo, label=label)
    labeled = [issue for issue in candidates if _issue_has_label(issue, label)]
    if labeled:
        return _newest_issue(labeled)
    hidden_state = [
        issue for issue in candidates if STATE_START in str(issue.get("body", ""))
    ]
    if hidden_state:
        return _newest_issue(hidden_state)
    exact_title = [
        issue for issue in candidates if str(issue.get("title", "")) == DASHBOARD_TITLE
    ]
    return _newest_issue(exact_title)


def _dashboard_issue_by_number(
    issue_repo: str, issue_number: int | None
) -> dict[str, Any] | None:
    if not issue_number:
        return None
    result = _run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            issue_repo,
            "--json",
            "number,title,url,labels,updatedAt,body,state",
        ],
        check=False,
        cli_scope="issue",
    )
    if result.returncode != 0:
        return None
    try:
        issue = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(issue, dict):
        return None
    if str(issue.get("state", "")).upper() != "OPEN":
        return None
    return issue


def _dashboard_issue_candidates(issue_repo: str, *, label: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    result = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            issue_repo,
            "--state",
            "open",
            "--label",
            label,
            "--json",
            "number,title,url,labels,updatedAt,body",
        ],
        check=False,
        cli_scope="issue",
    )
    if result.returncode == 0:
        issues.extend(_json_issue_list(result.stdout))
    title_result = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            issue_repo,
            "--state",
            "open",
            "--search",
            f'"{DASHBOARD_TITLE}" in:title',
            "--json",
            "number,title,url,labels,updatedAt,body",
        ],
        check=False,
        cli_scope="issue",
    )
    if title_result.returncode == 0:
        issues.extend(_json_issue_list(title_result.stdout))
    unique: dict[int, dict[str, Any]] = {}
    for issue in issues:
        try:
            unique[int(issue.get("number"))] = issue
        except (TypeError, ValueError):
            continue
    return list(unique.values())


def _json_issue_list(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    return (
        [issue for issue in data if isinstance(issue, dict)]
        if isinstance(data, list)
        else []
    )


def _newest_issue(issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not issues:
        return None
    return sorted(
        issues,
        key=lambda issue: str(issue.get("updatedAt", "")),
        reverse=True,
    )[0]


def _issue_has_label(issue: dict[str, Any], label: str) -> bool:
    return any(
        isinstance(item, dict) and item.get("name") == label
        for item in issue.get("labels", [])
    )


def _close_duplicate_dashboard_issues(
    issue_repo: str, *, canonical_number: int, title: str
) -> None:
    for issue in _dashboard_issue_candidates(issue_repo, label=DASHBOARD_LABEL):
        try:
            number = int(issue.get("number"))
        except (TypeError, ValueError):
            continue
        if number == canonical_number or str(issue.get("title", "")) != title:
            continue
        _run(
            [
                "gh",
                "issue",
                "close",
                str(number),
                "--repo",
                issue_repo,
                "--reason",
                "not planned",
            ],
            check=False,
            cli_scope="issue",
        )


def _ensure_label(issue_repo: str, *, label: str) -> None:
    _run(
        [
            "gh",
            "label",
            "create",
            label,
            "--repo",
            issue_repo,
            "--color",
            "0969da",
            "--description",
            "Central AIO fleet update dashboard",
        ],
        check=False,
        cli_scope="issue",
    )


def _add_dashboard_label(issue_repo: str, *, number: int, label: str) -> None:
    _run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            issue_repo,
            "--add-label",
            label,
        ],
        check=False,
        cli_scope="issue",
    )


def _dashboard_repo_refs(manifest: FleetManifest, group: str) -> list[DashboardRepoRef]:
    dashboard = manifest.raw.get("dashboard", {})
    if not isinstance(dashboard, dict):
        return []
    raw_group = dashboard.get(group, {})
    if not isinstance(raw_group, dict):
        return []
    refs: list[DashboardRepoRef] = []
    for name, config in raw_group.items():
        if not isinstance(config, dict):
            continue
        refs.append(
            DashboardRepoRef(
                name=str(name),
                github_repo=str(config.get("github_repo", f"{manifest.owner}/{name}")),
                path=Path(str(config.get("path", f"../{name}"))),
                raw=dict(config),
            )
        )
    return refs


def _pr_summary(pr: dict[str, Any], *, stale_days: int) -> dict[str, Any]:
    merge_state = str(pr.get("mergeStateStatus") or "UNKNOWN")
    check = _check_state(pr)
    age = _age_days(pr.get("createdAt"))
    state = "ready"
    if pr.get("isDraft"):
        state = "draft"
    elif merge_state in {"BLOCKED", "DIRTY", "BEHIND", "UNKNOWN"} or check in {
        "failure",
        "timed_out",
        "cancelled",
    }:
        state = "blocked"
    elif merge_state != "CLEAN":
        state = "attention"
    return {
        "number": pr.get("number", ""),
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "draft": bool(pr.get("isDraft")),
        "merge_state": merge_state,
        "check": check,
        "age_days": age,
        "stale": age >= stale_days,
        "state": state,
    }


def _issue_summary(issue: dict[str, Any], *, stale_days: int) -> dict[str, Any]:
    age = _age_days(issue.get("createdAt"))
    labels = [
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    ]
    normalized = {label.lower().replace("_", "-") for label in labels}
    needs_response = bool(
        normalized.intersection(
            {
                "needs-response",
                "needs-user-response",
                "needs-info",
                "question",
                "support",
            }
        )
    )
    return {
        "number": issue.get("number", ""),
        "title": issue.get("title", ""),
        "url": issue.get("url", ""),
        "labels": labels,
        "age_days": age,
        "stale": age >= stale_days,
        "needs_response": needs_response,
    }


def _dashboard_issue_rows(issue_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prioritized = sorted(
        issue_rows,
        key=lambda issue: (
            not bool(issue.get("needs_response")),
            not bool(issue.get("stale")),
            -_int(issue.get("age_days")),
        ),
    )
    return prioritized[:8]


def _top_issue_lines(rows: list[dict[str, Any]]) -> list[str]:
    issues: list[tuple[int, str]] = []
    for row in rows:
        repo = str(row.get("repo", ""))
        for issue in row.get("issues", []):
            if not isinstance(issue, dict):
                continue
            if not issue.get("needs_response") and not issue.get("stale"):
                continue
            age = _int(issue.get("age_days"))
            title = _cell(issue.get("title", ""))
            url = str(issue.get("url", ""))
            number = issue.get("number", "")
            label = f"{repo}#{number}: {title} ({age}d)"
            line = f"- [{label}]({url})" if url else f"- {label}"
            issues.append((age, line))
    issues.sort(key=lambda item: item[0], reverse=True)
    return [line for _, line in issues[:8]]


def _age_days(value: object) -> int:
    if not value:
        return 0
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    now = datetime.now(UTC)
    return max(0, (now - created).days)


def _git_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path_exists": False, "branch": "missing", "dirty": "unknown"}
    branch = _run(["git", "branch", "--show-current"], check=False, cwd=path)
    status = _run(["git", "status", "--short"], check=False, cwd=path)
    return {
        "path_exists": True,
        "branch": branch.stdout.strip() if branch.returncode == 0 else "unknown",
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else "unknown",
    }


def _git_head(path: Path) -> str:
    if not path.exists():
        return ""
    result = _run(["git", "rev-parse", "HEAD"], check=False, cwd=path)
    return result.stdout.strip() if result.returncode == 0 else ""


def _gh_json(args: list[str], *, cli_scope: str = "activity") -> Any:
    result = _run(["gh", *args], check=True, cli_scope=cli_scope)
    text = result.stdout.strip()
    return json.loads(text) if text else None


def _repo_activity_for_config(
    name: str, github_repo: str, raw: dict[str, Any], stale_days: int
) -> dict[str, Any]:
    if not _is_public_config(raw):
        return _private_activity(name)
    return repo_activity(name, github_repo, stale_days)


def _empty_activity_for_config(
    name: str, github_repo: str, raw: dict[str, Any]
) -> dict[str, Any]:
    if not _is_public_config(raw):
        return _private_activity(name)
    return _empty_activity(name, github_repo)


def _private_activity(name: str) -> dict[str, Any]:
    return {
        "repo": name,
        "github_repo": "",
        "activity_state": "private-skipped",
        "open_prs": "private",
        "open_issues": "private",
        "draft_prs": "private",
        "blocked_prs": "private",
        "clean_prs": "private",
        "stale_prs": "private",
        "oldest_pr_age_days": "private",
        "newest_issue_age_days": "private",
        "prs": [],
    }


def _empty_activity(name: str, github_repo: str) -> dict[str, Any]:
    return {
        "repo": name,
        "github_repo": github_repo,
        "activity_state": "skipped",
        "open_prs": "not-run",
        "open_issues": "not-run",
        "draft_prs": "not-run",
        "blocked_prs": "not-run",
        "clean_prs": "not-run",
        "stale_prs": "not-run",
        "oldest_pr_age_days": "not-run",
        "oldest_issue_age_days": "not-run",
        "newest_issue_age_days": "not-run",
        "oldest_pr": {},
        "oldest_issue": {},
        "prs": [],
        "issues": [],
        "needs_response_issues": "not-run",
    }


def _render_update_section(
    lines: list[str], title: str, rows: list[dict[str, Any]]
) -> None:
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["- none", ""])
        return
    lines.extend(
        [
            "| Repo | Component | Current | Latest | PR | Check | Signed | Safety | Config Delta | Template Impact | Runtime Smoke | Next |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {repo} | {component} | {current} | {latest} | {pr} | {check} | {signed} | {safety} | {config_delta} | {template_impact} | {runtime_smoke} | {next_action} |".format(
                **{key: _cell(row.get(key, "")) for key in row}
            )
        )
    lines.append("")


def _render_safety_review_section(lines: list[str], rows: list[dict[str, Any]]) -> None:
    update_rows = [row for row in rows if row.get("update")]
    lines.extend(["## Safety Review", ""])
    lines.extend(
        [
            "`ok` means no clear fleet-policy blocker was found. `warn` means human review is still required. `manual` means notify-only or not enough source data. `blocked` means fix before merge.",
            "",
        ]
    )
    if not update_rows:
        lines.extend(["- no upstream updates to review", ""])
        return
    lines.extend(
        [
            "| Repo | Component | Safety | Config | Runtime | Evidence | Next |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in update_rows:
        lines.append(
            "| {repo} | {component} | {safety} | {config_delta} | {runtime_smoke} | {evidence} | {next_action} |".format(
                repo=_cell(row.get("repo", "")),
                component=_cell(row.get("component", "")),
                safety=_cell(row.get("safety", "")),
                config_delta=_cell(row.get("config_delta", "")),
                runtime_smoke=_cell(row.get("runtime_smoke", "")),
                evidence=_cell(_safety_evidence(row)),
                next_action=_cell(row.get("next_action", "")),
            )
        )
    lines.append("")


def _render_destination_section(
    lines: list[str], destination_rows: list[dict[str, Any]]
) -> None:
    lines.extend(["## Destination Repo", ""])
    if not destination_rows:
        lines.extend(["- none configured", ""])
        return
    lines.extend(
        [
            "| Repo | Role | Catalog | Sync Queue | PRs | Issues | Next |",
            "| --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in destination_rows:
        lines.append(
            "| {repo} | {role} | {catalog_state} | {sync_queue_count} | {open_prs} | {open_issues} | {next_action} |".format(
                **{key: _cell(row.get(key, "")) for key in row}
            )
        )
    lines.extend(
        [
            "",
            "> Direct catalog edits should stay limited to catalog-only metadata or assets. App template changes start in the source repo.",
            "",
        ]
    )


def _render_rehab_section(lines: list[str], rehab_rows: list[dict[str, Any]]) -> None:
    lines.extend(["## Rehab / Onboarding", ""])
    if not rehab_rows:
        lines.extend(["- none configured", ""])
        return
    lines.extend(
        [
            "| Repo | Status | Branch | Dirty | Cleanup Findings | PRs | Issues | Next |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in rehab_rows:
        lines.append(
            "| {repo} | {status} | {branch} | {dirty} | {cleanup_findings} | {open_prs} | {open_issues} | {next_action} |".format(
                **{key: _cell(row.get(key, "")) for key in row}
            )
        )
    lines.append("")
    for row in rehab_rows:
        lines.append(f"**{row['repo']} first rehab checklist**")
        for item in row.get("checklist", []):
            lines.append(f"- [ ] {item}")
        lines.append("")


def _render_activity_section(
    lines: list[str],
    activity_rows: list[dict[str, Any]],
    destination_rows: list[dict[str, Any]],
    rehab_rows: list[dict[str, Any]],
) -> None:
    rows = [*activity_rows, *destination_rows, *rehab_rows]
    lines.extend(["## Fleet Activity", ""])
    if not rows:
        lines.extend(["- no activity collected", ""])
        return
    lines.extend(
        [
            "| Repo | PRs | Ready | Blocked | Draft | Stale | Issues | Needs Response | Oldest PR | Oldest Issue |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| {repo} | {open_prs} | {clean_prs} | {blocked_prs} | {draft_prs} | {stale_prs} | {open_issues} | {needs_response} | {oldest_pr_age} | {oldest_issue_age} |".format(
                repo=_cell(row.get("repo", "")),
                open_prs=_cell(row.get("open_prs", "")),
                clean_prs=_cell(row.get("clean_prs", "")),
                blocked_prs=_cell(row.get("blocked_prs", "")),
                draft_prs=_cell(row.get("draft_prs", "")),
                stale_prs=_cell(row.get("stale_prs", "")),
                open_issues=_cell(row.get("open_issues", "")),
                needs_response=_cell(row.get("needs_response_issues", "")),
                oldest_pr_age=_age_cell(row.get("oldest_pr_age_days")),
                oldest_issue_age=_age_cell(row.get("oldest_issue_age_days")),
            )
        )
    issue_lines = _top_issue_lines(rows)
    if issue_lines:
        lines.extend(["", "**Oldest / response-needed issues**"])
        lines.extend(issue_lines)
    lines.append("")


def _render_workflow_section(lines: list[str], workflow: dict[str, Any]) -> None:
    lines.extend(["## Control Plane Health", ""])
    if not workflow:
        lines.extend(["- workflow health was not collected", ""])
        return
    latest = workflow.get("latest") if isinstance(workflow.get("latest"), dict) else {}
    last_success = (
        workflow.get("last_success")
        if isinstance(workflow.get("last_success"), dict)
        else {}
    )
    last_failure = (
        workflow.get("last_failure")
        if isinstance(workflow.get("last_failure"), dict)
        else {}
    )
    lines.extend(
        [
            "| Workflow | State | Controls | Latest Run | Last Success | Last Failure |",
            "| --- | --- | --- | --- | --- | --- |",
            "| {workflow} | {state} | {controls} | {latest} | {last_success} | {last_failure} |".format(
                workflow=_cell(workflow.get("workflow", "")),
                state=_cell(workflow.get("state", "")),
                controls=_cell(
                    "enabled" if workflow.get("controls_enabled") else "disabled"
                ),
                latest=_run_link(latest),
                last_success=_run_link(last_success),
                last_failure=_run_link(last_failure),
            ),
            "",
        ]
    )


def _render_registry_section(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["## Registry Verification", ""])
    if not rows:
        lines.extend(["- not run", ""])
        return
    lines.extend(
        [
            "| Repo | Component | SHA | State | Docker Hub | GHCR | Verified |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {repo} | {component} | `{sha}` | {state} | {dockerhub} | {ghcr} | {verified_at} |".format(
                repo=_cell(row.get("repo", "")),
                component=_cell(row.get("component", "aio")),
                sha=_cell(str(row.get("sha", ""))[:12]),
                state=_cell(_registry_label(row)),
                dockerhub=_cell(len(row.get("dockerhub", []))),
                ghcr=_cell(len(row.get("ghcr", []))),
                verified_at=_cell(row.get("verified_at", "")),
            )
        )
    failures = [
        f"{row.get('repo')}:{row.get('component', 'aio')}: {failure}"
        for row in rows
        for failure in row.get("failures", [])
    ]
    if failures:
        lines.extend(["", "**Registry findings**"])
        lines.extend(f"- {failure}" for failure in failures[:10])
    lines.append("")


def _render_release_section(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(["## Release Queue", ""])
    if not rows:
        lines.extend(["- release readiness was not collected", ""])
        return
    lines.extend(
        [
            "| Repo | Component | State | Latest Tag | GitHub Release | Next Version | Next |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        github_release = row.get("latest_github_release", {})
        release_label = ""
        if isinstance(github_release, dict):
            release_label = str(
                github_release.get("tag")
                or github_release.get("state")
                or github_release.get("detail")
                or ""
            )
        lines.append(
            "| {repo} | {component} | {state} | {latest_release_tag} | {github_release} | {next_version} | {next_action} |".format(
                repo=_cell(row.get("repo", "")),
                component=_cell(row.get("component", "aio")),
                state=_cell(row.get("state", "")),
                latest_release_tag=_cell(row.get("latest_release_tag", "")),
                github_release=_cell(release_label),
                next_version=_cell(row.get("next_version", "")),
                next_action=_cell(row.get("next_action", "")),
            )
        )
    lines.append("")


def _render_cleanup_section(lines: list[str], rows: list[dict[str, Any]]) -> None:
    drift = [row for row in rows if row.get("findings_count")]
    local = [row for row in rows if row.get("local_findings_count")]
    lines.extend(["## Cleanup Drift", ""])
    if not rows:
        lines.extend(["- cleanup verification was not collected", ""])
        return
    if not drift and not local:
        lines.extend(["- no retired shared files found in active repos", ""])
        return
    if not drift:
        lines.extend(["- no retired shared files found in active repos", ""])
    else:
        lines.extend(["| Repo | Findings | First Finding |", "| --- | ---: | --- |"])
    for row in drift:
        findings = row.get("findings", [])
        first = findings[0] if isinstance(findings, list) and findings else {}
        label = (
            f"{first.get('path')}: {first.get('reason')}"
            if isinstance(first, dict)
            else ""
        )
        lines.append(
            "| {repo} | {findings_count} | {first} |".format(
                repo=_cell(row.get("repo", "")),
                findings_count=_cell(row.get("findings_count", 0)),
                first=_cell(label),
            )
        )
    if local:
        lines.extend(["", "Local-only hygiene:"])
        for row in local[:10]:
            findings = row.get("local_findings", [])
            first = findings[0] if isinstance(findings, list) and findings else {}
            label = (
                f"{first.get('path')}: {first.get('reason')}"
                if isinstance(first, dict)
                else ""
            )
            lines.append(f"- {_cell(row.get('repo', ''))}: {_cell(label)}")
    lines.append("")


def _render_fleet_state_section(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "## Update Queue",
            "",
            "| Repo | Component | Current | Latest | Strategy | PR | Check | Signed | Safety | Config Delta | Template Impact | Runtime Smoke | Registry | Release | Next |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {repo} | {component} | {current} | {latest} | {strategy} | {pr} | {check} | {signed} | {safety} | {config_delta} | {template_impact} | {runtime_smoke} | {registry} | {release} | {next_action} |".format(
                **{key: _cell(row.get(key, "")) for key in row}
            )
        )
    lines.append("")


def _render_controls(lines: list[str]) -> None:
    lines.extend(
        [
            "## Controls",
            "",
        ]
    )
    lines.extend(f"- [ ] {label}" for label in DASHBOARD_COMMANDS.values())
    lines.extend(
        [
            "",
            "> Check one box to trigger the central workflow. The dashboard rewrites this issue body in place and resets controls after the run.",
            "",
        ]
    )


def _render_next_commands(
    lines: list[str],
    rows: list[dict[str, Any]],
    destination_rows: list[dict[str, Any]],
    rehab_rows: list[dict[str, Any]],
) -> None:
    lines.extend(["## Next Commands", ""])
    commands: list[str] = []
    for row in rows:
        release_publish_handled = False
        release_detail = row.get("release_detail")
        if isinstance(release_detail, dict):
            state = release_detail.get("state")
            release_publish_handled = state in {
                "blocked",
                "publish-missing",
                "release-due",
            }
            operator_commands = release_detail.get("operator_commands")
            if isinstance(operator_commands, dict):
                control_check_publish = _actionable_control_check_publish_command(
                    release_detail
                )
                if state == "blocked":
                    command = release_detail.get("next_action")
                    if command:
                        commands.append(str(command))
                elif state == "publish-missing":
                    command = operator_commands.get("registry_verify")
                    if command:
                        commands.append(str(command))
                    if control_check_publish:
                        commands.append(control_check_publish)
                elif state == "release-due" and control_check_publish:
                    commands.append(control_check_publish)
        registry_detail = row.get("registry_detail")
        if (
            isinstance(registry_detail, dict)
            and registry_detail.get("failures")
            and not release_publish_handled
        ):
            repo = row.get("repo")
            component = row.get("component", "aio")
            sha = registry_detail.get("sha") or "<sha>"
            commands.append(
                fleet_command(
                    "registry",
                    "verify",
                    "--repo",
                    repo,
                    "--component",
                    component,
                    "--sha",
                    sha,
                    "--verbose",
                )
            )
            commands.append(
                fleet_command(
                    "registry", "publish", "--repo", repo, "--component", component
                )
            )
        if row.get("update") and row.get("strategy") == "notify":
            commands.append(
                fleet_command(
                    "upstream", "assess", "--repo", row["repo"], "--format", "json"
                )
            )
        elif row.get("update") and row.get("safety") in {"warn", "blocked"}:
            pr_url = _markdown_url(row.get("pr"))
            pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1] if pr_url else ""
            if pr_number.isdigit():
                commands.append(
                    fleet_command(
                        "upstream",
                        "assess",
                        "--repo",
                        row["repo"],
                        "--pr",
                        pr_number,
                        "--format",
                        "json",
                    )
                )
            else:
                commands.append(
                    fleet_command(
                        "upstream", "assess", "--repo", row["repo"], "--format", "json"
                    )
                )
        elif _is_ready_update(row):
            pr_url = _markdown_url(row.get("pr"))
            if pr_url:
                commands.append(f"gh pr view {pr_url}")
    if destination_rows:
        commands.append(
            fleet_command("validate-catalog", "--catalog-path", "../awesome-unraid")
        )
    for row in rehab_rows:
        commands.append(
            fleet_command("onboard-repo", "--repo", row["repo"], "--mode", "rehab")
        )
    if not commands:
        lines.extend(["- no immediate commands", ""])
        return
    lines.append("```bash")
    lines.extend(dict.fromkeys(commands))
    lines.append("```")
    lines.append("")


def _actionable_control_check_publish_command(release_detail: dict[str, Any]) -> str:
    state = str(release_detail.get("state", ""))
    if state not in {"publish-missing", "release-due"}:
        return ""
    sha = str(release_detail.get("sha", "")).strip()
    if not FULL_SHA_RE.fullmatch(sha):
        return ""
    operator_commands = release_detail.get("operator_commands")
    if not isinstance(operator_commands, dict):
        return ""
    command = str(operator_commands.get("release_transaction", "")).strip()
    if command:
        if "<sha>" in command:
            return ""
        if f"--sha {sha}" not in command:
            return ""
        command_parts = command.split()
        if "release" not in command_parts or "transaction" not in command_parts:
            return ""
        if "--dry-run" not in command_parts and "--write" not in command_parts:
            return ""
        return command

    command = str(operator_commands.get("control_check_publish", "")).strip()
    if not command or "<sha>" in command:
        return ""
    if f"--sha {sha}" not in command:
        return ""
    command_parts = command.split()
    if "--publish" not in command_parts or "--publish-component" not in command_parts:
        return ""
    return command


def _is_ready_update(row: dict[str, Any]) -> bool:
    return (
        bool(row.get("update"))
        and row.get("next_action") == "human review and merge"
        and row.get("check") == "success"
        and row.get("signed") == "verified"
        and row.get("safety") in {"ok", "not-needed", ""}
    )


def _is_triage_update(row: dict[str, Any]) -> bool:
    return (
        bool(row.get("update"))
        and not _is_blocked_update(row)
        and (row.get("strategy") == "notify" or row.get("safety") in {"warn", "manual"})
    )


def _is_blocked_update(row: dict[str, Any]) -> bool:
    if not row.get("update") or row.get("strategy") == "notify":
        return False
    if row.get("safety") == "blocked":
        return True
    if row.get("safety") in {"warn", "manual"}:
        return False
    action = str(row.get("next_action", ""))
    return action != "human review and merge"


def _safety_evidence(row: dict[str, Any]) -> str:
    for key in ("safety_failures", "safety_warnings", "safety_signals"):
        values = row.get(key, [])
        if isinstance(values, list) and values:
            return str(values[0])
    return "no clear blocker found"


def _summary_keys() -> list[str]:
    return [
        "active_repos",
        "destination_repos",
        "rehab_repos",
        "upstream_updates",
        "ready_updates",
        "triage_updates",
        "blocked_updates",
        "registry_failures",
        "sha_tag_missing",
        "release_due",
        "publish_missing",
        "cleanup_findings",
        "open_prs",
        "open_issues",
        "needs_response_issues",
        "alert_warnings",
    ]


def _posture(summary: dict[str, Any]) -> str:
    if (
        _int(summary.get("blocked_updates"))
        or _int(summary.get("blocked_prs"))
        or _int(summary.get("registry_failures"))
        or _int(summary.get("publish_missing"))
        or summary.get("workflow_state") in {"failure", "cancelled", "timed_out"}
    ):
        return "blocked"
    if (
        _int(summary.get("upstream_updates"))
        or _int(summary.get("release_due"))
        or _int(summary.get("cleanup_findings"))
        or _int(summary.get("stale_prs"))
        or _int(summary.get("needs_response_issues"))
    ):
        return "action required"
    if _int(summary.get("alert_warnings")):
        return "watch"
    return "green"


def _remote_posture(summary: dict[str, Any]) -> str:
    scoped = dict(summary)
    scoped["local_hygiene"] = 0
    return _posture(scoped)


def _issue_number_from_url(url: str) -> int | None:
    try:
        return int(url.rstrip("/").rsplit("/", 1)[1])
    except (IndexError, ValueError):
        return None


def _run(
    command: list[str],
    *,
    check: bool = True,
    cwd: Path | None = None,
    cli_scope: str = "activity",
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _github_cli_env(cli_scope) if command and command[0] == "gh" else None
    result = subprocess.run(  # nosec B603
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        input=input_text,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result


def _github_cli_env(cli_scope: str) -> dict[str, str] | None:
    token = _github_cli_token(cli_scope)
    if not token:
        return None
    env = os.environ.copy()
    for key in GITHUB_CLI_TOKEN_KEYS:
        env.pop(key, None)
    env["GH_TOKEN"] = token
    return env


def _github_cli_token(cli_scope: str) -> str:
    keys = (
        ("AIO_FLEET_ISSUE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
        if cli_scope == "issue"
        else (
            "AIO_FLEET_DASHBOARD_TOKEN",
            "AIO_FLEET_UPSTREAM_TOKEN",
            "AIO_FLEET_CHECK_TOKEN",
            "APP_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        )
    )
    for key in keys:
        token = os.environ.get(key, "").strip()
        if token:
            return token
    return ""


def _cell(value: object) -> str:
    text = str(value).replace("\n", " ").replace("|", "\\|").strip()
    return text or "-"


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _age_cell(value: object) -> str:
    return f"{value}d" if isinstance(value, int) else _cell(value)


def _run_link(run: dict[str, Any]) -> str:
    if not run:
        return "-"
    label = str(run.get("conclusion") or run.get("status") or run.get("id") or "run")
    url = str(run.get("url") or "")
    return f"[{_cell(label)}]({url})" if url else _cell(label)


def _markdown_url(value: object) -> str:
    text = str(value or "")
    if "](" not in text:
        return text if text.startswith("http") else ""
    return text.split("](", 1)[1].rstrip(")")
