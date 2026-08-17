from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aio_fleet.public_text import assert_public_text, public_text_safe_value

FLEET_REPORT_SCHEMA_VERSION = 4
FLEET_REPORT_TOP_LEVEL_KEYS = (
    "schema_version",
    "generated_at",
    "issue_repo",
    "warnings",
    "summary",
    "rows",
    "actions",
    "failures",
    "approvals",
    "catalog",
    "standards",
    "candidates",
    "activity",
    "destination_repos",
    "rehab_repos",
    "registry",
    "releases",
    "cleanup",
    "workflow",
)


@dataclass(frozen=True)
class FleetReport:
    """Versioned report envelope shared by dashboard, alerts, and future UIs."""

    generated_at: str
    issue_repo: str
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    actions: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    catalog: dict[str, Any] = field(default_factory=dict)
    standards: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, Any] = field(default_factory=dict)
    activity: list[dict[str, Any]] = field(default_factory=list)
    destination_repos: list[dict[str, Any]] = field(default_factory=list)
    rehab_repos: list[dict[str, Any]] = field(default_factory=list)
    registry: list[dict[str, Any]] = field(default_factory=list)
    releases: list[dict[str, Any]] = field(default_factory=list)
    cleanup: list[dict[str, Any]] = field(default_factory=list)
    workflow: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = FLEET_REPORT_SCHEMA_VERSION

    def to_state(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "issue_repo": self.issue_repo,
            "warnings": self.warnings,
            "summary": self.summary,
            "rows": self.rows,
            "actions": self.actions,
            "failures": self.failures,
            "approvals": self.approvals,
            "catalog": self.catalog,
            "standards": self.standards,
            "candidates": self.candidates,
            "activity": self.activity,
            "destination_repos": self.destination_repos,
            "rehab_repos": self.rehab_repos,
            "registry": self.registry,
            "releases": self.releases,
            "cleanup": self.cleanup,
            "workflow": self.workflow,
        }


def stable_report_json(state: dict[str, Any]) -> str:
    """Render report JSON in a deterministic form for snapshots and consumers."""

    return json.dumps(state, indent=2, sort_keys=True)


def public_fleet_report_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return report state safe for public dashboard/report surfaces."""

    safe_state = public_text_safe_value(state)
    if not isinstance(safe_state, dict):
        safe_state = {}
    assert_public_text(stable_report_json(safe_state), context="fleet-report state")
    return safe_state


def public_fleet_report_json(state: dict[str, Any]) -> str:
    """Render deterministic report JSON after public-text sanitization."""

    text = stable_report_json(public_fleet_report_state(state))
    assert_public_text(text, context="fleet-report output")
    return text


def fleet_report_json_schema() -> dict[str, Any]:
    """Return the stable top-level schema consumed by future UI surfaces."""

    object_map = {"type": "object", "additionalProperties": True}
    array_map = {"type": "array", "items": object_map}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/wgross19/aio-fleet/schemas/fleet-report-v4.json",
        "title": "AIO Fleet Report",
        "type": "object",
        "additionalProperties": False,
        "required": list(FLEET_REPORT_TOP_LEVEL_KEYS),
        "properties": {
            "schema_version": {"const": FLEET_REPORT_SCHEMA_VERSION},
            "generated_at": {"type": "string"},
            "issue_repo": {"type": "string"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "summary": object_map,
            "rows": array_map,
            "actions": array_map,
            "failures": array_map,
            "approvals": array_map,
            "catalog": object_map,
            "standards": object_map,
            "candidates": object_map,
            "activity": array_map,
            "destination_repos": array_map,
            "rehab_repos": array_map,
            "registry": array_map,
            "releases": array_map,
            "cleanup": array_map,
            "workflow": object_map,
        },
    }


def validate_report_shape(state: dict[str, Any]) -> list[str]:
    """Validate the versioned report envelope without pulling in jsonschema."""

    failures: list[str] = []
    for key in FLEET_REPORT_TOP_LEVEL_KEYS:
        if key not in state:
            failures.append(f"missing top-level key: {key}")
    extra = sorted(set(state) - set(FLEET_REPORT_TOP_LEVEL_KEYS))
    for key in extra:
        failures.append(f"unexpected top-level key: {key}")
    if state.get("schema_version") != FLEET_REPORT_SCHEMA_VERSION:
        failures.append(
            f"unsupported schema_version: {state.get('schema_version')!r}; expected {FLEET_REPORT_SCHEMA_VERSION}"
        )
    for key in (
        "warnings",
        "rows",
        "actions",
        "failures",
        "approvals",
        "activity",
        "destination_repos",
        "rehab_repos",
        "registry",
        "releases",
        "cleanup",
    ):
        if key in state and not isinstance(state[key], list):
            failures.append(f"{key}: expected list")
    for key in ("summary", "workflow", "catalog", "standards", "candidates"):
        if key in state and not isinstance(state[key], dict):
            failures.append(f"{key}: expected object")
    for key in ("generated_at", "issue_repo"):
        if key in state and not isinstance(state[key], str):
            failures.append(f"{key}: expected string")
    return failures
