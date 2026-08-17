from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import uuid
from pathlib import Path
from typing import Any

from aio_fleet.changelog import build_release_plan, component_config
from aio_fleet.command_text import fleet_command
from aio_fleet.control_plane import publish_components
from aio_fleet.manifest import RepoConfig
from aio_fleet.release_plan import (
    control_check_publish_command,
    release_plan_for_repo,
    release_plan_rows_for_repo,
    release_transaction_command,
)
from aio_fleet.signing import current_generated_pr_signature_blockers

FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

TRANSACTION_PHASES = [
    "refresh-truth",
    "preflight",
    "source-pr",
    "source-checks",
    "source-merge",
    "release-prepare",
    "release-pr",
    "release-checks",
    "release-merge",
    "publish-control-check",
    "registry-verify",
    "github-release",
    "catalog-sync",
    "final-verify",
]


def release_transaction_preflight(
    repo: RepoConfig,
    *,
    components: list[str] | None = None,
    expected_sha: str = "",
    event: str = "push",
    write: bool = False,
    require_credentials: bool = False,
    required_checks_passed: bool = False,
    mode: str = "transaction",
) -> dict[str, Any]:
    selected_components = _selected_components(repo, components)
    head = _git_head(repo.path)
    effective_sha = expected_sha or head
    findings: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if expected_sha and not FULL_SHA_RE.fullmatch(expected_sha):
        findings.append(
            _finding(
                "checkout-mismatch",
                f"expected SHA must be a full commit SHA, got {expected_sha}",
            )
        )
    if expected_sha and FULL_SHA_RE.fullmatch(expected_sha) and head != expected_sha:
        findings.append(
            _finding(
                "checkout-mismatch",
                f"app checkout HEAD {head or '<unknown>'} does not match expected {expected_sha}",
            )
        )

    findings.extend(_checkout_findings(repo.path))
    findings.extend(
        _submodule_policy_findings(repo, event=event, components=selected_components)
    )
    findings.extend(_release_metadata_findings(repo, components=selected_components))
    findings.extend(_generated_pr_signature_findings(repo))

    if write:
        findings.extend(_autopilot_findings(repo, components=selected_components))
        if not required_checks_passed:
            findings.append(
                _finding(
                    "required-check-missing",
                    "write mode requires a required-check success attestation",
                )
            )
    else:
        for component in selected_components:
            policy = release_transaction_policy(repo, component)
            if policy["checkout_policy"] == "trusted-submodules":
                warnings.append(
                    _warning(
                        "submodule-policy-mismatch",
                        f"{repo.name}:{component} requires trusted submodule checkout for heavy validation",
                    )
                )

    if require_credentials or write:
        findings.extend(_credential_findings(repo))

    failure_classes = sorted({finding["class"] for finding in findings})
    component_policies = {
        component: release_transaction_policy(repo, component)
        for component in selected_components
    }
    report = {
        "status": "blocked" if findings else "ok",
        "mode": mode,
        "repo": repo.name,
        "components": selected_components,
        "expected_sha": effective_sha,
        "head": head,
        "event": event,
        "write_requested": write,
        "required_checks_passed": required_checks_passed,
        "failure_classes": failure_classes,
        "findings": findings,
        "warnings": warnings,
        "policies": component_policies,
        "operator_commands": _operator_commands(
            repo, components=selected_components, sha=effective_sha
        ),
    }
    return report


def release_transaction_report(
    repo: RepoConfig,
    *,
    components: list[str] | None = None,
    expected_sha: str = "",
    event: str = "push",
    write: bool = False,
    dry_run: bool = True,
    transaction_id: str = "",
    require_credentials: bool = False,
    required_checks_passed: bool = False,
) -> dict[str, Any]:
    selected_components = _selected_components(repo, components)
    transaction_id = transaction_id or _transaction_id(repo.name, selected_components)
    preflight = release_transaction_preflight(
        repo,
        components=selected_components,
        expected_sha=expected_sha,
        event=event,
        write=write,
        require_credentials=require_credentials,
        required_checks_passed=required_checks_passed,
    )
    plans = _release_plans(repo, components=selected_components)
    actionable = [
        plan
        for plan in plans
        if str(plan.get("state", ""))
        in {"release-due", "publish-missing", "catalog-sync-needed"}
    ]
    phases = _transaction_phases(preflight=preflight, dry_run=dry_run)
    status = "blocked" if preflight["status"] == "blocked" else "ready"
    if not actionable and status == "ready":
        status = "ok"
    return {
        "transaction_id": transaction_id,
        "status": status,
        "dry_run": dry_run,
        "write_requested": write,
        "repo": repo.name,
        "components": selected_components,
        "expected_sha": preflight["expected_sha"],
        "event": event,
        "failure_classes": preflight["failure_classes"],
        "preflight": preflight,
        "release_plan": {
            "repos": plans,
            "summary": {
                "repos": len(plans),
                "actionable": len(actionable),
                "publish_missing": len(
                    [
                        plan
                        for plan in plans
                        if str(plan.get("state", "")) == "publish-missing"
                    ]
                ),
                "release_due": len(
                    [
                        plan
                        for plan in plans
                        if str(plan.get("state", "")) == "release-due"
                    ]
                ),
                "catalog_sync_needed": len(
                    [
                        plan
                        for plan in plans
                        if str(plan.get("state", "")) == "catalog-sync-needed"
                    ]
                ),
            },
        },
        "phases": phases,
        "operator_commands": preflight["operator_commands"],
    }


def release_transaction_resume_report(transaction_id: str) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "status": "blocked",
        "failure_classes": ["transaction-state-missing"],
        "findings": [
            _finding(
                "transaction-state-missing",
                "resume requires a saved transaction report path; rerun release transaction for the repo/component",
            )
        ],
    }


def release_transaction_policy(repo: RepoConfig, component: str) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "autopilot": False,
        "autopilot_explicit": False,
        "checkout_policy": (
            "trusted-submodules" if repo.raw.get("checkout_submodules") else "standard"
        ),
        "publish_policy": "central-control",
    }

    def _merge_release_transaction_config(config: dict[str, Any]) -> None:
        for key in ["autopilot", "checkout_policy", "publish_policy"]:
            if key in config:
                policy[key] = config[key]
        if config.get("autopilot") is True:
            policy["autopilot_explicit"] = True

    default_policy = repo.defaults.get("release_transaction")
    if isinstance(default_policy, dict):
        for key in ["checkout_policy", "publish_policy"]:
            if key in default_policy:
                policy[key] = default_policy[key]
    repo_policy = repo.raw.get("release_transaction")
    if isinstance(repo_policy, dict):
        _merge_release_transaction_config(repo_policy)
    config_policy = component_config(repo, component).get("release_transaction")
    if isinstance(config_policy, dict):
        _merge_release_transaction_config(config_policy)
    policy["autopilot"] = bool(policy.get("autopilot") is True)
    return policy


def _release_plans(repo: RepoConfig, *, components: list[str]) -> list[dict[str, Any]]:
    if repo.publish_profile == "template":
        return release_plan_rows_for_repo(repo, include_registry=False)
    return [
        release_plan_for_repo(repo, include_registry=False, component=component)
        for component in components
    ]


def _selected_components(repo: RepoConfig, components: list[str] | None) -> list[str]:
    selected = [component for component in (components or []) if component]
    if selected:
        return selected
    if repo.publish_profile == "template":
        return ["template"]
    return publish_components(repo)


def _transaction_id(repo: str, components: list[str]) -> str:
    slug = "-".join([repo, *components]).replace("_", "-")
    return f"{slug}-{uuid.uuid4().hex[:12]}"


def _transaction_phases(
    *, preflight: dict[str, Any], dry_run: bool
) -> list[dict[str, str]]:
    blocked = preflight["status"] == "blocked"
    phases: list[dict[str, str]] = []
    for name in TRANSACTION_PHASES:
        status = "pending"
        if name == "preflight":
            status = "blocked" if blocked else "passed"
        elif blocked:
            status = "blocked"
        elif dry_run:
            status = "planned"
        phases.append({"name": name, "status": status})
    return phases


def _checkout_findings(path: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    branch = _git_stdout(["git", "branch", "--show-current"], cwd=path)
    if not branch:
        findings.append(_finding("checkout-mismatch", "app checkout is detached"))

    status = _git_stdout(["git", "status", "--short"], cwd=path)
    if status:
        findings.append(
            _finding(
                "checkout-mismatch",
                "app checkout is dirty before release transaction",
            )
        )
    uv_lock_status = _git_stdout(
        ["git", "status", "--short", "--", "uv.lock"], cwd=path
    )
    if uv_lock_status:
        findings.append(
            _finding(
                "checkout-mismatch",
                "unexpected uv.lock drift is present; rerun release tooling from aio-fleet",
            )
        )

    drift = _git_stdout(
        ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
        cwd=path,
    )
    if drift:
        parts = drift.split()
        if len(parts) >= 2 and parts[1] != "0":
            findings.append(
                _finding(
                    "checkout-mismatch",
                    f"app checkout is behind origin/main by {parts[1]} commit(s)",
                )
            )
    return findings


def _submodule_policy_findings(
    repo: RepoConfig, *, event: str, components: list[str]
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if event != "pull_request":
        return findings
    for component in components:
        policy = release_transaction_policy(repo, component)
        if policy["checkout_policy"] == "trusted-submodules":
            findings.append(
                _finding(
                    "submodule-policy-mismatch",
                    f"{repo.name}:{component} requires trusted submodule checkout; use a trusted push or workflow_dispatch transaction",
                )
            )
    return findings


def _release_metadata_findings(
    repo: RepoConfig, *, components: list[str]
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for component in components:
        try:
            plan = build_release_plan(repo, component=component)
        except (Exception, SystemExit) as exc:
            findings.append(
                _finding(
                    "version-drift",
                    f"{repo.name}:{component} release metadata cannot be planned: {exc}",
                )
            )
            continue
        if not plan.version:
            findings.append(
                _finding(
                    "version-drift",
                    f"{repo.name}:{component} release version is empty",
                )
            )
        if not plan.changelog_path.exists():
            findings.append(
                _finding(
                    "version-drift",
                    f"{repo.name}:{component} changelog is missing: {plan.changelog_path.name}",
                )
            )
        missing_xml = [path.name for path in plan.xml_paths if not path.exists()]
        if missing_xml:
            findings.append(
                _finding(
                    "version-drift",
                    f"{repo.name}:{component} release XML is missing: {', '.join(missing_xml)}",
                )
            )
    return findings


def _autopilot_findings(
    repo: RepoConfig, *, components: list[str]
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if repo.raw.get("public") is not True:
        findings.append(
            _finding(
                "permission-gap",
                f"{repo.name} must be public in fleet.yml before release transaction write mode",
            )
        )
    for component in components:
        policy = release_transaction_policy(repo, component)
        if not policy["autopilot_explicit"]:
            findings.append(
                _finding(
                    "permission-gap",
                    f"{repo.name}:{component} requires release_transaction.autopilot: true before write mode",
                )
            )
    return findings


def _credential_findings(repo: RepoConfig) -> list[dict[str, str]]:
    if repo.publish_profile == "template":
        return []
    missing = [
        name
        for name in [
            "DOCKERHUB_USERNAME",
            "DOCKERHUB_TOKEN",
            "AIO_FLEET_GHCR_TOKEN",
        ]
        if not os.environ.get(name)
    ]
    findings = (
        [_finding("credential-gap", "missing " + ", ".join(missing))] if missing else []
    )
    if not os.environ.get("DOCKERHUB_DELETE_TOKEN"):
        findings.append(
            _finding(
                "delete-scope-gap",
                "missing DOCKERHUB_DELETE_TOKEN for tag cleanup readiness",
            )
        )
    return findings


def _generated_pr_signature_findings(repo: RepoConfig) -> list[dict[str, str]]:
    if repo.raw.get("public") is not True:
        return []
    return [
        _finding("unsigned-generated-pr", blocker)
        for blocker in current_generated_pr_signature_blockers(
            repo.github_repo, repo.path
        )
    ]


def _operator_commands(
    repo: RepoConfig, *, components: list[str], sha: str
) -> dict[str, Any]:
    return {
        "preflight": [
            fleet_command(
                "release",
                "preflight",
                "--repo",
                repo.name,
                "--component",
                component,
                "--sha",
                sha or "<sha>",
                "--mode",
                "transaction",
                "--format",
                "json",
            )
            for component in components
        ],
        "transaction": [
            release_transaction_command(repo, component=component, sha=sha)
            for component in components
        ],
        "control_check_publish": [
            control_check_publish_command(repo, component=component, sha=sha)
            for component in components
            if repo.publish_profile != "template"
        ],
        "registry_verify": [
            _registry_verify_command(repo, component=component, sha=sha)
            for component in components
            if repo.publish_profile != "template"
        ],
    }


def _registry_verify_command(repo: RepoConfig, *, component: str, sha: str) -> str:
    label_sha = sha if sha else "<sha>"
    return fleet_command(
        "registry",
        "verify",
        "--repo",
        repo.name,
        "--component",
        component,
        "--sha",
        label_sha,
        "--verbose",
    )


def _git_head(path: Path) -> str:
    return _git_stdout(["git", "rev-parse", "HEAD"], cwd=path)


def _git_stdout(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _finding(cls: str, detail: str) -> dict[str, str]:
    return {"class": cls, "message": f"{cls}: {detail}"}


def _warning(cls: str, detail: str) -> dict[str, str]:
    return {"class": cls, "message": f"{cls}: {detail}"}
