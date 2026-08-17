from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from aio_fleet.alerts import alert_payload, emit_alert, payload_from_report
from aio_fleet.app_manifest import (
    APP_MANIFEST_NAME,
    app_manifest_from_repo,
    load_app_manifest,
    render_app_manifest,
)
from aio_fleet.catalog import sync_catalog_assets, unpublished_xml_targets
from aio_fleet.catalog_changelog import (
    catalog_changelog_drift,
    render_catalog_changelog,
)
from aio_fleet.catalog_workflow import (
    RETIRED_CATALOG_PATHS,
    catalog_workflow_findings,
    current_aio_fleet_ref,
    render_validate_catalog_workflow,
    resolve_aio_fleet_ref,
    write_validate_catalog_workflow,
)
from aio_fleet.change_scope import (
    CHECK_MODE_FAST_CLEANUP,
    CHECK_MODE_FULL,
    ChangeScope,
    classify_required_check_scope,
)
from aio_fleet.changelog import (
    build_release_plan,
    component_config,
    normalize_markdown_changelog,
    release_section_exists,
    update_registry_revision_arg,
    update_template_changes,
    write_temp_git_cliff_config,
)
from aio_fleet.checks import (
    CHECK_NAME,
    check_external_id,
    check_run_payload,
    check_run_satisfied,
    upsert_check_run,
)
from aio_fleet.cleanup import CleanupFinding, cleanup_findings, remove_cleanup_findings
from aio_fleet.command_text import fleet_command
from aio_fleet.control_plane import (
    _secret_environment_key,
    central_check_steps,
    publish_components,
    registry_publish_command,
    run_central_trunk,
    run_steps,
)
from aio_fleet.doctor import fleet_doctor_report, manifest_shape_checks
from aio_fleet.failure_classifier import classify_failure_text
from aio_fleet.fleet_dashboard import (
    dashboard_issue_commands,
    dashboard_report,
    upsert_dashboard_issue,
)
from aio_fleet.fleet_queue import (
    action_by_id,
    build_action_queue,
    dispatch_plan,
    enrich_command_center_state,
)
from aio_fleet.fleetbot import render_command_response
from aio_fleet.github_cli import github_cli_env
from aio_fleet.github_policy import load_policy, validate_github_policy
from aio_fleet.hooks import install_local_hooks, run_local_trunk_overlay
from aio_fleet.manifest import FleetManifest, ManifestError, RepoConfig, load_manifest
from aio_fleet.poll import poll_targets, resolve_changed_files
from aio_fleet.public_text import assert_public_text, redact_public_text
from aio_fleet.registry import (
    component_registry_release_tag,
    compute_registry_tags,
    delete_dockerhub_tags,
    dockerhub_auth_preflight_failure,
    dockerhub_delete_scope_preflight_failure,
    registry_sha_tag_required,
    verify_registry_tags,
)
from aio_fleet.release import (
    extract_release_notes,
    find_release_publish_target_commit,
    git,
    latest_changelog_version,
    latest_component_changelog_version,
    latest_component_release_tag,
    read_upstream_version,
)
from aio_fleet.release_plan import (
    control_check_publish_command,
    release_plan_for_manifest,
    release_plan_for_repo,
    release_plan_rows_for_repo,
)
from aio_fleet.release_transaction import (
    release_transaction_preflight,
    release_transaction_report,
    release_transaction_resume_report,
)
from aio_fleet.report import (
    fleet_report_json_schema,
    public_fleet_report_json,
    public_fleet_report_state,
    stable_report_json,
    validate_report_shape,
)
from aio_fleet.safety import assess_expected_update, assess_upstream_pr
from aio_fleet.signing import signing_doctor_report
from aio_fleet.smoke import smoke_published_images
from aio_fleet.upstream import (
    create_or_update_upstream_pr,
    monitor_repo,
    result_dict,
)
from aio_fleet.validators import (
    TRACKED_ARTIFACT_PATTERNS,
    catalog_asset_failures,
    catalog_quality_findings,
    catalog_repo_failures,
    derived_repo_failures,
    pinned_action_failures,
    repo_local_workflow_failures,
    repo_policy_failures,
    template_metadata_failures,
    tracked_artifact_failures,
)
from aio_fleet.workflow_jobs import (
    apply_upstream_monitor_actions,
    checkout_dashboard_repos,
    checkout_upstream_monitor_repos,
    poll_outputs,
    registry_audit_checkouts,
    render_registry_summary,
    render_upstream_summary,
    upstream_monitor_checkouts,
    upstream_poll_targets,
    validate_upstream_monitor_report,
)
from aio_fleet.workflow_security import audit_workflows

FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
OnboardingPack = dict[
    str,
    list[dict[str, object]] | list[str] | dict[str, object],
]


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        cmd, cwd=cwd, env=env, check=False, text=True, capture_output=True
    )


def _run_streaming(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        cmd,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
    )


def _current_ref() -> str:
    result = _run(["git", "rev-parse", "HEAD"])
    if result.returncode != 0:
        return "main"
    return result.stdout.strip()


def _git_head(repo_path: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=repo_path)
    if result.returncode != 0:
        raise ManifestError(f"unable to resolve HEAD in {repo_path}")
    return result.stdout.strip()


def _repo_for_identifier(manifest: FleetManifest, identifier: str) -> RepoConfig:
    if identifier in manifest.repos:
        return manifest.repo(identifier)
    matches = [repo for repo in manifest.repos.values() if repo.app_slug == identifier]
    if len(matches) == 1:
        return matches[0]
    raise ManifestError(f"unknown repo or app slug in fleet.yml: {identifier}")


def _repo_with_path(repo: RepoConfig, path: Path) -> RepoConfig:
    raw = dict(repo.raw)
    raw["path"] = str(path)
    return RepoConfig(name=repo.name, raw=raw, defaults=repo.defaults, owner=repo.owner)


def _app_manifest_failures(repo: RepoConfig) -> list[str]:
    path = repo.path / APP_MANIFEST_NAME
    if not path.exists():
        return [f"{repo.name}: missing {APP_MANIFEST_NAME}"]
    failures: list[str] = []
    try:
        actual = load_app_manifest(path)
    except ManifestError as exc:
        failures.append(f"{repo.name}: {APP_MANIFEST_NAME} invalid: {exc}")
        return failures

    expected = app_manifest_from_repo(repo)
    if actual != expected:
        failures.append(
            f"{repo.name}: {APP_MANIFEST_NAME} drifted from fleet.yml; run aio-fleet export-app-manifest --repo {repo.name} --write"
        )
    return failures


def cmd_doctor(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    checks = fleet_doctor_report(
        manifest,
        repos=args.repo,
        include_local=not args.no_local,
        include_app_checks=args.app_checks,
        include_publish=args.publish,
        include_cleanup=args.cleanup,
        include_alerts=args.alerts,
        live_auth=args.live_auth,
        check_delete_scope=args.check_delete_scope,
        require_alerts=args.require_alerts,
    )["checks"]
    if not args.no_manifest_checks:
        selected_repos = set(args.repo or [])
        checks.extend(
            check
            for check in manifest_shape_checks(manifest)
            if not selected_repos or check.get("repo") in selected_repos
        )
        for name, repo in manifest.repos.items():
            if selected_repos and name not in selected_repos:
                continue
            if not repo.path.exists():
                continue
            for failure in [
                *_app_manifest_failures(repo),
                *catalog_asset_failures(repo),
                *tracked_artifact_failures(repo.path),
            ]:
                checks.append(
                    {
                        "name": "manifest-validation",
                        "status": "failed",
                        "class": "manifest-drift",
                        "repo": name,
                        "detail": failure,
                    }
                )
    if args.github:
        for failure in validate_github_policy(
            Path(args.policy),
            check_secrets=args.check_secrets,
        ):
            checks.append(
                {
                    "name": "github-policy",
                    "status": "failed",
                    "class": "github-policy",
                    "detail": failure,
                }
            )
    failed = [check for check in checks if check["status"] == "failed"]
    warnings = [check for check in checks if check["status"] == "warning"]
    report = {
        "status": "failed" if failed else "ok",
        "failure_classes": sorted(
            {str(check["class"]) for check in failed if check.get("class")}
        ),
        "checks": checks,
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "warnings": len(warnings),
        },
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(
                "{name}: {status}: {detail}".format(
                    name=check["name"],
                    status=check["status"],
                    detail=check["detail"],
                )
            )
    if failed:
        return 1
    if args.format != "json":
        print(f"fleet doctor ok: {len(manifest.repos)} repos")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    policy_repos: set[str] = set()
    catalog_path = Path(args.catalog_path).resolve() if args.catalog_path else None
    if args.github:
        try:
            policy_repos = set(load_policy(Path(args.policy))["repositories"])
        except Exception:
            policy_repos = set()
    for name, repo in manifest.repos.items():
        branch = _run(["git", "branch", "--show-current"], cwd=repo.path)
        status = _run(["git", "status", "--short"], cwd=repo.path)
        dirty = "dirty" if status.stdout.strip() else "clean"
        drift = _run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
            cwd=repo.path,
        )
        drift_state = ""
        ahead = "?"
        behind = "?"
        if drift.returncode == 0:
            ahead, behind = (drift.stdout.strip().split() + ["0", "0"])[:2]
            drift_state = f" ahead={ahead} behind={behind}"
        current_pr_state = ""
        open_pr_state = ""
        policy_state = ""
        branch_name = branch.stdout.strip()
        if args.github and branch_name:
            current_pr = _run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo.github_repo,
                    "--head",
                    branch_name,
                    "--json",
                    "number,url,isDraft,statusCheckRollup",
                    "--jq",
                    (
                        '.[0] // null | if . == null then "no-pr" '
                        'else "#\\(.number) \\(.url) draft=\\(.isDraft) checks=\\(.statusCheckRollup | length)" end'
                    ),
                ],
                cwd=repo.path,
            )
            current_pr_state = (
                f" current_pr={current_pr.stdout.strip() or 'pr-unknown'}"
            )
            open_prs = _run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo.github_repo,
                    "--state",
                    "open",
                    "--json",
                    "number",
                    "--jq",
                    "length",
                ],
                cwd=repo.path,
            )
            open_pr_state = f" open_prs={open_prs.stdout.strip() or 'unknown'}"
            if name not in policy_repos:
                policy_state = " policy=manual"
            else:
                try:
                    failures = validate_github_policy(
                        Path(args.policy), repos=[name], check_secrets=False
                    )
                    policy_state = (
                        " policy=ok"
                        if not failures
                        else f" policy={len(failures)}-drift"
                    )
                except Exception as exc:
                    policy_state = f" policy=unknown:{exc}"
        catalog_state = f" {_catalog_status(repo, catalog_path)}"
        publish_state = f" {_publish_status(repo, dirty=dirty, behind=behind, policy_state=policy_state)}"
        print(
            f"{name:22} {branch_name or '-':36} "
            f"{dirty}{drift_state}{current_pr_state}{open_pr_state}{policy_state}{catalog_state}{publish_state}"
        )
    return 0


def _catalog_status(repo: RepoConfig, catalog_path: Path | None) -> str:
    if repo.raw.get("catalog_published") is False:
        return "catalog=held"
    if catalog_path is None:
        return "catalog=published"

    missing = [
        target
        for target in _catalog_asset_targets(repo)
        if target and not (catalog_path / target).exists()
    ]
    if missing:
        return f"catalog=missing:{','.join(missing)}"
    return "catalog=ok"


def _catalog_asset_targets(repo: RepoConfig) -> list[str]:
    assets = repo.raw.get("catalog_assets", [])
    if not isinstance(assets, list):
        return []
    return [
        str(asset.get("target", "")).strip()
        for asset in assets
        if isinstance(asset, dict) and str(asset.get("target", "")).strip()
    ]


def _publish_status(
    repo: RepoConfig, *, dirty: str, behind: str, policy_state: str
) -> str:
    if repo.publish_profile == "template":
        return "publish=manual"
    if dirty != "clean":
        return "publish=blocked:dirty"
    if behind not in {"0", "?"}:
        return "publish=blocked:behind"
    if policy_state.startswith(" policy=") and policy_state != " policy=ok":
        return "publish=blocked:policy"
    return "publish=source-ready"


def cmd_debt_report(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    catalog_path = Path(args.catalog_path).resolve() if args.catalog_path else None
    catalog_failures = (
        catalog_repo_failures(manifest, catalog_path) if catalog_path else []
    )
    report: dict[str, object] = {
        "ref": "control-plane",
        "catalog_path": str(catalog_path) if catalog_path else None,
        "repos": [],
    }
    for repo in manifest.repos.values():
        git_status = _run(["git", "status", "--short"], cwd=repo.path)
        dirty = "dirty" if git_status.stdout.strip() else "clean"
        drift = _run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
            cwd=repo.path,
        )
        ahead = behind = "?"
        if drift.returncode == 0:
            ahead, behind = (drift.stdout.strip().split() + ["0", "0"])[:2]

        retired_shared_paths = [
            str(finding.path.relative_to(repo.path))
            for finding in cleanup_findings(repo)
        ]
        repo_catalog_failures = [
            failure
            for failure in catalog_failures
            if failure.startswith(f"{repo.name}:")
        ]
        github_policy_failures: list[str] = []
        if args.github:
            try:
                github_policy_failures = validate_github_policy(
                    Path(args.policy), repos=[repo.name], check_secrets=False
                )
            except Exception as exc:
                github_policy_failures = [f"{repo.name}: github policy unknown: {exc}"]
        trunk_state = _trunk_state(repo.path) if args.trunk else "not-run"
        open_prs = _open_prs(repo) if args.github else "not-run"
        repo_report = {
            "repo": repo.name,
            "path": str(repo.path),
            "dirty": dirty,
            "ahead": ahead,
            "behind": behind,
            "publish": _publish_status(
                repo,
                dirty=dirty,
                behind=behind,
                policy_state=(
                    " policy=ok" if not github_policy_failures else " policy=drift"
                ),
            ),
            "retired_shared_paths": retired_shared_paths,
            "catalog_failures": repo_catalog_failures,
            "pinned_action_failures": pinned_action_failures(repo.path),
            "tracked_artifacts": tracked_artifact_failures(repo.path),
            "untracked_artifacts": _untracked_artifacts(repo.path),
            "github_policy_failures": github_policy_failures,
            "open_prs": open_prs,
            "trunk": trunk_state,
            "dify_launch": _dify_launch_state(repo, repo_catalog_failures),
        }
        report["repos"].append(repo_report)  # type: ignore[index]

    repos = report["repos"]  # type: ignore[assignment]
    report["summary"] = {
        "repos": len(repos),
        "retired_shared_paths": sum(
            bool(item["retired_shared_paths"]) for item in repos
        ),
        "catalog_failures": len(catalog_failures),
        "tracked_artifacts": sum(bool(item["tracked_artifacts"]) for item in repos),
        "untracked_artifacts": sum(bool(item["untracked_artifacts"]) for item in repos),
    }

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.format == "markdown":
        print(_debt_report_markdown(report))
    else:
        print(_debt_report_text(report))
    return 0


def cmd_standards_reconcile(args: argparse.Namespace) -> int:
    if getattr(args, "dry_run", False) and args.write:
        print(
            "standards reconcile: --dry-run cannot be combined with --write",
            file=sys.stderr,
        )
        return 1
    manifest = load_manifest(Path(args.manifest))
    selected = set(args.repo or [])
    repos = [
        repo
        for repo in manifest.repos.values()
        if not selected or repo.name in selected
    ]
    actions: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []
    cleanup_by_repo: dict[str, list[CleanupFinding]] = {}
    for repo in repos:
        actions.extend(_standards_manifest_actions(repo))
        cleanup = cleanup_findings(repo)
        cleanup_by_repo[repo.name] = cleanup
        actions.extend(_standards_cleanup_actions(repo, cleanup))
        if args.github:
            actions.extend(_standards_github_actions(repo, Path(args.policy)))
        if args.release:
            actions.extend(
                _standards_release_actions(repo, include_registry=args.registry)
            )
    destinations = _public_destination_paths(manifest, selected)
    aio_fleet_ref = ""
    if destinations:
        aio_fleet_ref = resolve_aio_fleet_ref(Path.cwd())
        for name, path in destinations.items():
            actions.extend(
                _standards_catalog_destination_actions(name, path, aio_fleet_ref)
            )

    if args.write:
        applied_cleanup_repos: set[str] = set()
        for action in actions:
            if action["kind"] == "app-manifest":
                repo = manifest.repo(str(action["repo"]))
                output = repo.path / APP_MANIFEST_NAME
                output.write_text(render_app_manifest(repo))
                applied.append({**action, "applied": True})
            elif action["kind"] == "cleanup":
                repo_name = str(action["repo"])
                if repo_name not in applied_cleanup_repos:
                    remove_cleanup_findings(cleanup_by_repo.get(repo_name, []))
                    applied_cleanup_repos.add(repo_name)
                applied.append({**action, "applied": True})
            elif action["kind"] == "catalog-workflow":
                destination = destinations[str(action["repo"])]
                write_validate_catalog_workflow(
                    destination, aio_fleet_ref=aio_fleet_ref
                )
                applied.append({**action, "applied": True})
            elif action["kind"] == "catalog-cleanup":
                destination = destinations[str(action["repo"])]
                relative = str(action.get("path", ""))
                target = destination / relative
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                applied.append({**action, "applied": True})

    actionable = [
        action for action in actions if action.get("severity") in {"failure", "warning"}
    ]
    report = {
        "status": "actionable" if actionable else "ok",
        "actions": actions,
        "applied": applied,
        "summary": {
            "repos": len(repos) + len(destinations),
            "actions": len(actions),
            "actionable": len(actionable),
            "applied": len(applied),
            "by_kind": _count_by_key(actions, "kind"),
            "by_class": _count_by_key(actions, "class"),
        },
    }
    if args.format == "json":
        print(stable_report_json(report))
    else:
        if not actions:
            print("standards reconcile: no drift")
        for action in actions:
            print(
                "{repo}: {severity}: {class_name}: {detail}".format(
                    repo=action["repo"],
                    severity=action["severity"],
                    class_name=action["class"],
                    detail=action["detail"],
                )
            )
            command = str(action.get("command", "") or "")
            if command:
                print(f"- {command}")
        if applied:
            print(f"applied: {len(applied)} safe local fix(es)")
    return 1 if actionable and not args.allow_drift else 0


def _public_destination_paths(
    manifest: FleetManifest, selected: set[str]
) -> dict[str, Path]:
    destinations = (
        manifest.raw.get("dashboard", {}).get("destination_repos", {})
        if isinstance(manifest.raw.get("dashboard", {}), dict)
        else {}
    )
    if not isinstance(destinations, dict):
        return {}
    paths: dict[str, Path] = {}
    for name, config in sorted(destinations.items()):
        if selected and name not in selected:
            continue
        if not isinstance(config, dict) or config.get("public") is not True:
            continue
        path = str(config.get("path", "")).strip()
        if path:
            paths[str(name)] = Path(path)
    return paths


def _standards_catalog_destination_actions(
    name: str, path: Path, aio_fleet_ref: str
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for relative, reason in RETIRED_CATALOG_PATHS.items():
        target = path / relative
        if not target.exists():
            continue
        provenance = (
            "remote-confirmed" if _git_tracks_path(path, relative) else "local-only"
        )
        actions.append(
            _standards_raw_action(
                repo=name,
                kind="catalog-cleanup",
                cls="retired-catalog-path",
                severity="info" if provenance == "local-only" else "failure",
                provenance=provenance,
                detail=f"{relative}: {reason}",
                command=f"python -m aio_fleet standards reconcile --repo {name} --write",
                can_write=True,
                path=relative,
            )
        )

    for finding in catalog_workflow_findings(path, aio_fleet_ref=aio_fleet_ref):
        if any(
            finding.startswith(f"{relative}:") for relative in RETIRED_CATALOG_PATHS
        ):
            continue
        actions.append(
            _standards_raw_action(
                repo=name,
                kind="catalog-workflow",
                cls="catalog-workflow-drift",
                severity="failure",
                detail=finding,
                command=(
                    "python -m aio_fleet catalog-workflow "
                    f"--catalog-path {shlex.quote(str(path))} --write"
                ),
                can_write=True,
            )
        )
    return actions


def _git_tracks_path(repo_path: Path, relative: str) -> bool:
    result = _run(["git", "ls-files", "--", relative], cwd=repo_path)
    return result.returncode == 0 and bool(result.stdout.strip())


def _standards_manifest_actions(repo: RepoConfig) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for failure in _app_manifest_failures(repo):
        actions.append(
            _standards_action(
                repo,
                kind="app-manifest",
                cls="manifest-drift",
                severity="failure",
                detail=failure,
                command=fleet_command(
                    "export-app-manifest", "--repo", repo.name, "--write"
                ),
                can_write=True,
            )
        )
    return actions


def _standards_cleanup_actions(
    repo: RepoConfig, findings: list[CleanupFinding]
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for finding in findings:
        relative = str(finding.path.relative_to(repo.path))
        provenance = finding.provenance
        actions.append(
            _standards_action(
                repo,
                kind="cleanup",
                cls="retired-shared-path",
                severity="info" if provenance == "local-only" else "failure",
                detail=f"{relative}: {finding.reason}",
                command=fleet_command(
                    "cleanup-repo", "--repo", repo.name, "--fix", "--verify"
                ),
                can_write=True,
                provenance=provenance,
            )
        )
    return actions


def _standards_github_actions(
    repo: RepoConfig, policy_path: Path
) -> list[dict[str, object]]:
    try:
        failures = validate_github_policy(
            policy_path, repos=[repo.name], check_secrets=False
        )
    except Exception as exc:
        failures = [f"{repo.name}: github policy unknown: {exc}"]
    return [
        _standards_action(
            repo,
            kind="github-policy",
            cls="github-policy",
            severity="failure",
            detail=failure,
            command=fleet_command("validate-github", "--repo", repo.name),
            can_write=False,
        )
        for failure in failures
    ]


def _standards_release_actions(
    repo: RepoConfig, *, include_registry: bool
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if not repo.path.is_dir():
        return [_standards_missing_release_checkout_action(repo)]
    try:
        rows = release_plan_rows_for_repo(repo, include_registry=include_registry)
    except FileNotFoundError:
        return [_standards_missing_release_checkout_action(repo)]
    for row in rows:
        state = str(row.get("state", ""))
        if state in {"current", "private-skipped"}:
            continue
        severity = "failure" if state in {"publish-missing", "blocked"} else "warning"
        component = str(row.get("component", "aio"))
        actions.append(
            _standards_action(
                repo,
                kind="release",
                cls=f"release-{state}",
                severity=severity,
                detail=(
                    f"{repo.name}:{component} release state {state}; "
                    f"warnings={row.get('warnings', [])}; blockers={row.get('blockers', [])}"
                ),
                command=str(row.get("next_action", "") or ""),
                can_write=False,
                component=component,
            )
        )
    return actions


def _standards_missing_release_checkout_action(repo: RepoConfig) -> dict[str, object]:
    return _standards_action(
        repo,
        kind="release",
        cls="release-checkout-missing",
        severity="warning",
        detail="checkout path missing; release state unavailable",
        command="",
        can_write=False,
        provenance="local-missing",
    )


def _standards_action(
    repo: RepoConfig,
    *,
    kind: str,
    cls: str,
    severity: str,
    detail: str,
    command: str,
    can_write: bool,
    component: str = "",
    provenance: str = "remote-confirmed",
) -> dict[str, object]:
    return _standards_raw_action(
        repo=repo.name,
        component=component,
        kind=kind,
        cls=cls,
        severity=severity,
        provenance=provenance,
        detail=detail,
        command=command,
        can_write=can_write,
    )


def _standards_raw_action(
    *,
    repo: str,
    kind: str,
    cls: str,
    severity: str,
    detail: str,
    command: str,
    can_write: bool,
    component: str = "",
    provenance: str = "remote-confirmed",
    path: str = "",
) -> dict[str, object]:
    return {
        "repo": repo,
        "component": component,
        "kind": kind,
        "class": cls,
        "severity": severity,
        "provenance": provenance,
        "detail": detail,
        "command": command,
        "can_write": can_write,
        **({"path": path} if path else {}),
    }


def _count_by_key(items: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, "") or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _untracked_artifacts(repo_path: Path) -> list[str]:
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo_path
    )
    if result.returncode != 0:
        return [f"unable to inspect untracked files: {result.stderr.strip()}"]
    artifacts: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        if any(fnmatch.fnmatch(path, pattern) for pattern in TRACKED_ARTIFACT_PATTERNS):
            artifacts.append(path)
    return artifacts


def _trunk_state(repo_path: Path) -> str:
    if not (repo_path / ".trunk" / "trunk.yaml").exists():
        return "skipped:no-config"
    result = _run(
        [
            "trunk",
            "check",
            "--show-existing",
            "--all",
            "--no-fix",
            "--no-progress",
            "--color=false",
        ],
        cwd=repo_path,
    )
    if result.returncode == 0:
        return "ok"
    issue_line = next(
        (
            line.strip()
            for line in result.stdout.splitlines()
            if "issues" in line.lower()
        ),
        "",
    )
    return f"failed:{issue_line or result.returncode}"


def _open_prs(repo: RepoConfig) -> str:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            "length",
        ],
        cwd=repo.path,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _dify_launch_state(repo: RepoConfig, catalog_failures: list[str]) -> str:
    if repo.name != "dify-aio":
        return "not-applicable"
    if repo.raw.get("catalog_published") is not True:
        return "held"
    if catalog_failures:
        return "blocked:catalog"
    return "catalog-ready"


def _debt_report_text(report: dict[str, object]) -> str:
    lines = ["AIO fleet debt report"]
    for item in report["repos"]:  # type: ignore[index]
        problems = []
        for key in [
            "retired_shared_paths",
            "catalog_failures",
            "pinned_action_failures",
            "tracked_artifacts",
            "untracked_artifacts",
            "github_policy_failures",
        ]:
            if item[key]:  # type: ignore[index]
                problems.append(key)
        status = "ok" if not problems else ",".join(problems)
        lines.append(
            f"{item['repo']}: {status} {item['publish']} trunk={item['trunk']} open_prs={item['open_prs']}"  # type: ignore[index]
        )
    lines.append(f"summary: {report['summary']}")
    return "\n".join(lines)


def _debt_report_markdown(report: dict[str, object]) -> str:
    lines = [
        "# AIO Fleet Debt Report",
        "",
        "| Repo | Status | Publish | Trunk | Open PRs |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report["repos"]:  # type: ignore[index]
        problems = []
        for key in [
            "retired_shared_paths",
            "catalog_failures",
            "pinned_action_failures",
            "tracked_artifacts",
            "untracked_artifacts",
            "github_policy_failures",
        ]:
            if item[key]:  # type: ignore[index]
                problems.append(key.replace("_", " "))
        status = "ok" if not problems else ", ".join(problems)
        lines.append(
            f"| {item['repo']} | {status} | {item['publish']} | {item['trunk']} | {item['open_prs']} |"  # type: ignore[index]
        )
    return "\n".join(lines)


def cmd_validate_actions(args: argparse.Namespace) -> int:
    failures = pinned_action_failures(Path(args.repo_path).resolve())
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("All workflow actions are pinned to full commit SHAs.")
    return 0


def cmd_verify_caller(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    repo_at_path = _repo_with_path(repo, Path(args.repo_path).resolve())
    failures = repo_local_workflow_failures(repo_at_path)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"{repo.name} caller policy checks passed")
    return 0


def cmd_validate_derived(args: argparse.Namespace) -> int:
    failures = derived_repo_failures(
        Path(args.repo_path).resolve(),
        strict_placeholders=args.strict_placeholders,
        template_xml=args.template_xml or os.environ.get("TEMPLATE_XML"),
    )
    if failures:
        print(
            "\n".join(f"template validation error: {failure}" for failure in failures),
            file=sys.stderr,
        )
        return 1
    print("Derived repo validation passed.")
    return 0


def cmd_validate_repo(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    repo_at_path = _repo_with_path(repo, Path(args.repo_path).resolve())
    failures = repo_policy_failures(repo_at_path, manifest)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"{repo.name} fleet policy checks passed")
    return 0


def cmd_validate_template_common(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    if args.all and args.repo_path:
        print("--repo-path can only be used with --repo", file=sys.stderr)
        return 1
    repos = (
        list(manifest.repos.values())
        if args.all
        else [_repo_for_identifier(manifest, args.repo)]
    )
    failures: list[str] = []
    for repo in repos:
        repo_to_check = (
            _repo_with_path(repo, Path(args.repo_path).resolve())
            if args.repo_path
            else repo
        )
        failures.extend(template_metadata_failures(repo_to_check, manifest))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    checked = len(repos)
    print(f"common template validation passed for {checked} repo(s)")
    return 0


def cmd_validate_catalog(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    failures = catalog_repo_failures(manifest, Path(args.catalog_path).resolve())
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("catalog checks passed")
    return 0


def cmd_validate_github(args: argparse.Namespace) -> int:
    failures = validate_github_policy(
        Path(args.policy),
        repos=args.repo,
        check_secrets=args.check_secrets,
    )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("github policy checks passed")
    return 0


def cmd_check_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    conclusion = args.conclusion
    if args.status == "completed" and conclusion is None:
        conclusion = "success"
    payload = check_run_payload(
        repo,
        sha=args.sha,
        event=args.event,
        status=args.status,
        conclusion=conclusion,
        summary=args.summary,
        details_url=args.details_url,
    )
    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    try:
        result = upsert_check_run(
            repo,
            sha=args.sha,
            event=args.event,
            status=args.status,
            conclusion=conclusion,
            summary=args.summary,
            details_url=args.details_url,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "action": result.action,
                "check_run_id": result.check_run_id,
                "html_url": result.html_url,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    targets = poll_targets(
        manifest,
        include_prs=not args.no_prs,
        include_main=not args.no_main,
    )
    emitted: list[dict[str, object]] = []
    for target in targets:
        if args.missing_checks_only and check_run_satisfied(
            target.repo, sha=target.sha, event=target.event
        ):
            continue
        row = {
            "repo": target.repo.name,
            "sha": target.sha,
            "event": target.event,
            "source": target.source,
            "checkout_submodules": target.checkout_submodules,
            "publish": target.publish,
            "publish_components": list(target.publish_components),
            "check_mode": target.check_mode,
            "changed_paths": list(target.changed_paths),
            "changed_files": list(target.changed_files),
            "fast_path_reason": target.fast_path_reason,
        }
        emitted.append(row)
        if args.create_checks:
            if args.dry_run:
                row["check_payload"] = check_run_payload(
                    target.repo,
                    sha=target.sha,
                    event=target.event,
                    status="queued",
                    summary=f"Queued from aio-fleet poll source {target.source}",
                )
            else:
                result = upsert_check_run(
                    target.repo,
                    sha=target.sha,
                    event=target.event,
                    status="queued",
                    summary=f"Queued from aio-fleet poll source {target.source}",
                )
                row["check_run"] = {
                    "action": result.action,
                    "check_run_id": result.check_run_id,
                    "html_url": result.html_url,
                }
    if args.format == "json":
        print(json.dumps({"targets": emitted}, indent=2, sort_keys=True))
    else:
        for row in emitted:
            print(f"{row['repo']} {row['source']} {row['event']} {row['sha']}")
        print(f"poll targets: {len(emitted)}")
    return 0


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FAILURE_EXCERPT_MAX_LINES = 5
_FAILURE_EXCERPT_MAX_CHARS = 900


def _failure_file_annotations(paths: list[str] | None) -> list[str]:
    annotations: list[str] = []
    for raw_path in paths or []:
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        excerpt = _failure_excerpt(text)
        if excerpt:
            annotations.append(f"failure excerpt {path.name}: {excerpt}")
    return annotations


def _failure_excerpt(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _ANSI_ESCAPE_RE.sub("", raw_line)
        line = _CONTROL_TEXT_RE.sub("", line).strip()
        if not line:
            continue
        lines.append(redact_public_text(line))
        if len(lines) >= _FAILURE_EXCERPT_MAX_LINES:
            break
    excerpt = " | ".join(lines)
    if len(excerpt) > _FAILURE_EXCERPT_MAX_CHARS:
        excerpt = excerpt[: _FAILURE_EXCERPT_MAX_CHARS - 3].rstrip() + "..."
    return excerpt


def cmd_alert_send(args: argparse.Namespace) -> int:
    report = None
    if args.report_json:
        report = json.loads(Path(args.report_json).read_text())
    annotations = list(getattr(args, "annotation", None) or [])
    annotations.extend(_failure_file_annotations(getattr(args, "failure_file", None)))

    if report is not None:
        payload = payload_from_report(
            event=args.event,
            report=report,
            status=args.status,
            summary=args.summary,
            details_url=args.details_url or "",
            dedupe_key=args.dedupe_key or "",
        )
        if annotations:
            payload = replace(
                payload,
                annotations=[*payload.annotations, *annotations],
            )
    else:
        status = "success" if args.status == "auto" else args.status
        payload = alert_payload(
            event=args.event,
            status=status,
            summary=args.summary,
            repo=args.repo or "",
            component=args.component or "",
            details_url=args.details_url or "",
            dedupe_key=args.dedupe_key or "",
            annotations=annotations,
        )

    result = emit_alert(
        payload,
        kuma_url=args.kuma_url or os.environ.get("AIO_FLEET_KUMA_PUSH_URL", ""),
        webhook_url=args.webhook_url
        or os.environ.get("AIO_FLEET_ALERT_WEBHOOK_URL", ""),
        webhook_format=args.webhook_format,
        force_webhook=args.force_webhook,
        dry_run=args.dry_run,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{payload.event}: alert={payload.status}")
        print(f"kuma: {result['kuma']}")
        print(f"webhook: {result['webhook']}")
    return 0


def cmd_alert_doctor(args: argparse.Namespace) -> int:
    kuma_url = args.kuma_url or os.environ.get("AIO_FLEET_KUMA_PUSH_URL", "")
    webhook_url = args.webhook_url or os.environ.get("AIO_FLEET_ALERT_WEBHOOK_URL", "")
    findings: list[str] = []
    warnings: list[str] = []
    if not webhook_url:
        warnings.append("AIO_FLEET_ALERT_WEBHOOK_URL is not configured")
    if args.require_alerts:
        findings.extend(warnings)
        warnings = []
    report = {
        "kuma": "configured" if kuma_url else "disabled",
        "webhook": "configured" if webhook_url else "missing",
        "warnings": warnings,
        "findings": findings,
        "ok": not findings,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"alert kuma={report['kuma']} webhook={report['webhook']}")
        for warning in warnings:
            print(f"- warning: {warning}")
        for finding in findings:
            print(f"- {finding}")
    return 1 if findings else 0


def cmd_alert_test(args: argparse.Namespace) -> int:
    payload = alert_payload(
        event=args.event,
        status=args.status,
        summary=args.summary,
        repo=args.repo or "",
        component=args.component or "",
        dedupe_key=args.dedupe_key or "alert-test:fleet:all",
        details_url=args.details_url or "",
        annotations=["aio-fleet alert test"],
    )
    result = emit_alert(
        payload,
        kuma_url=args.kuma_url or os.environ.get("AIO_FLEET_KUMA_PUSH_URL", ""),
        webhook_url=args.webhook_url
        or os.environ.get("AIO_FLEET_ALERT_WEBHOOK_URL", ""),
        webhook_format=args.webhook_format,
        force_webhook=True,
        dry_run=args.dry_run,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"{payload.event}: alert-test={payload.status}")
        print(f"kuma: {result['kuma']}")
        print(f"webhook: {result['webhook']}")
    return 0


def cmd_fleet_dashboard_update(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    dashboard_config = manifest.raw.get("dashboard", {})
    configured_issue_number = (
        dashboard_config.get("issue_number")
        if isinstance(dashboard_config, dict)
        else None
    )
    report = dashboard_report(
        manifest,
        include_registry=args.registry,
        include_activity=getattr(args, "include_activity", True),
        stale_days=getattr(args, "stale_days", 7),
        issue_repo=args.issue_repo,
    )
    result = upsert_dashboard_issue(
        issue_repo=args.issue_repo,
        body=str(report["body"]),
        issue_number=args.issue_number or configured_issue_number,
        dry_run=not args.write,
    )
    output = {
        "action": result.action,
        "issue_number": result.number,
        "issue_url": result.url,
        "state": report["state"],
    }
    if args.format == "json":
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"fleet-dashboard: {result.action}")
        if result.url:
            print(result.url)
        if not args.write:
            print(str(report["body"]))
    return 0


def cmd_fleet_dashboard_commands(args: argparse.Namespace) -> int:
    result = dashboard_issue_commands(
        issue_repo=args.issue_repo,
        issue_number=args.issue_number,
    )
    if args.format == "github-output":
        commands = result.get("commands", {})
        print(f"is_dashboard={str(bool(result.get('is_dashboard'))).lower()}")
        print(f"requested={str(bool(result.get('requested'))).lower()}")
        print(f"rescan={str(bool(commands.get('rescan'))).lower()}")
        print(
            "upstream_monitor=" f"{str(bool(commands.get('upstream_monitor'))).lower()}"
        )
        print(
            "standards_reconcile="
            f"{str(bool(commands.get('standards_reconcile'))).lower()}"
        )
        print(
            "queue_publish_checks="
            f"{str(bool(commands.get('queue_publish_checks'))).lower()}"
        )
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _report_state_from_payload(payload: object) -> dict[str, object]:
    if isinstance(payload, dict) and isinstance(payload.get("state"), dict):
        return dict(payload["state"])
    if isinstance(payload, dict):
        return dict(payload)
    raise ManifestError("report input must be a JSON object")


def cmd_fleet_report_generate(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    report = dashboard_report(
        manifest,
        include_registry=args.registry,
        include_activity=getattr(args, "include_activity", True),
        stale_days=getattr(args, "stale_days", 7),
        issue_repo=args.issue_repo,
    )
    state = public_fleet_report_state(dict(report["state"]))
    if args.format == "json":
        print(public_fleet_report_json(state))
    else:
        summary = state.get("summary", {})
        posture = (
            summary.get("posture", "unknown")
            if isinstance(summary, dict)
            else "unknown"
        )
        print(f"fleet-report: posture={posture} schema={state.get('schema_version')}")
    return 0


def cmd_fleet_report_closeout(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    report = dashboard_report(
        manifest,
        include_registry=args.registry,
        include_activity=getattr(args, "include_activity", True),
        stale_days=getattr(args, "stale_days", 7),
        issue_repo=args.issue_repo,
    )
    state = public_fleet_report_state(dict(report["state"]))
    closeout = _fleet_closeout_state(state)
    if args.format == "json":
        print(public_fleet_report_json(closeout))
    else:
        print(
            "fleet-closeout: remote={remote} local={local} actions={actions}".format(
                remote=closeout["remote_posture"],
                local=closeout["local_posture"],
                actions=closeout["summary"]["remote_actions"],
            )
        )
    return 0


def _fleet_closeout_state(state: dict[str, object]) -> dict[str, object]:
    summary = state.get("summary")
    summary = dict(summary) if isinstance(summary, dict) else {}
    actions = [
        action
        for action in _dict_items(state.get("actions"))
        if action.get("provenance") != "local-only"
    ]
    cleanup_rows = _dict_items(state.get("cleanup"))
    local_hygiene = [
        {
            "repo": row.get("repo", ""),
            "state": "local-only",
            "findings_count": row.get("local_findings_count", 0),
            "findings": row.get("local_findings", []),
            "cleanup_command": fleet_command(
                "cleanup-repo",
                "--repo",
                row.get("repo", ""),
                "--fix",
                "--verify",
            ),
        }
        for row in cleanup_rows
        if int(row.get("local_findings_count", 0) or 0)
    ]
    output = {
        "schema_version": state.get("schema_version"),
        "generated_at": state.get("generated_at", ""),
        "remote_posture": summary.get(
            "remote_posture", summary.get("posture", "unknown")
        ),
        "local_posture": summary.get(
            "local_posture", "hygiene" if local_hygiene else "clean"
        ),
        "summary": {
            "open_prs": summary.get("open_prs", 0),
            "upstream_updates": summary.get("upstream_updates", 0),
            "release_due": summary.get("release_due", 0),
            "publish_missing": summary.get("publish_missing", 0),
            "catalog_state": summary.get("catalog_state", "unknown"),
            "standards_state": summary.get("standards_state", "unknown"),
            "workflow_state": summary.get("workflow_state", "unknown"),
            "remote_actions": len(actions),
            "pending_approvals": summary.get("pending_approvals", 0),
            "alert_warnings": summary.get("alert_warnings", 0),
            "local_hygiene": summary.get("local_hygiene", len(local_hygiene)),
        },
        "remote_actions": actions,
        "local_hygiene": local_hygiene,
        "warnings": state.get("warnings", []),
    }
    return public_fleet_report_state(output)


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def cmd_fleet_report_schema(args: argparse.Namespace) -> int:
    print(stable_report_json(fleet_report_json_schema()))
    return 0


def cmd_fleet_report_validate(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(Path(args.input).read_text())
        state = _report_state_from_payload(payload)
    except Exception as exc:
        print(f"invalid report input: {exc}", file=sys.stderr)
        return 1
    failures = validate_report_shape(state)
    if args.format == "json":
        print(stable_report_json({"ok": not failures, "failures": failures}))
    else:
        if failures:
            print("\n".join(failures), file=sys.stderr)
        else:
            print("fleet report schema ok")
    return 1 if failures else 0


def cmd_fleet_report_explain_run(args: argparse.Namespace) -> int:
    if getattr(args, "log_file", ""):
        text = Path(args.log_file).read_text()
        command_error = ""
    else:
        result = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "run",
                "view",
                str(args.run_id),
                "--repo",
                args.issue_repo,
                "--log-failed",
            ],
            check=False,
            text=True,
            capture_output=True,
            env=github_cli_env(
                (
                    "AIO_FLEET_DASHBOARD_TOKEN",
                    "AIO_FLEET_CHECK_TOKEN",
                    "AIO_FLEET_WORKFLOW_TOKEN",
                    "APP_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                )
            ),
        )
        text = "\n".join(part for part in (result.stdout, result.stderr) if part)
        command_error = (
            ""
            if result.returncode == 0
            else "gh run view failed; raw output omitted from report"
        )
    classification = classify_failure_text(
        text,
        metadata={"run_id": str(args.run_id), "repo": args.issue_repo},
    )
    output = {
        "run_id": str(args.run_id),
        "repo": args.issue_repo,
        "classification": classification,
        "command_error": command_error,
    }
    if args.format == "json":
        print(stable_report_json(output))
    else:
        print(
            "run {run_id}: {root_cause} confidence={confidence}".format(
                run_id=args.run_id,
                root_cause=classification["root_cause"],
                confidence=classification["confidence"],
            )
        )
        print(classification["next_action"])
    return 0


def cmd_fleet_queue_generate(args: argparse.Namespace) -> int:
    state = _command_center_state(args)
    actions = build_action_queue(state)
    output = {
        "schema_version": state.get("schema_version"),
        "generated_at": state.get("generated_at", ""),
        "actions": actions,
        "summary": {
            "actions": len(actions),
            "requires_approval": len(
                [action for action in actions if action.get("requires_approval")]
            ),
        },
    }
    if args.format == "json":
        print(stable_report_json(output))
    else:
        print(
            "fleet-queue: actions={actions} approvals={approvals}".format(
                actions=output["summary"]["actions"],
                approvals=output["summary"]["requires_approval"],
            )
        )
    return 0


def cmd_fleet_queue_dispatch(args: argparse.Namespace) -> int:
    state = _command_center_state(args)
    actions = build_action_queue(state)
    try:
        action = action_by_id(actions, args.id)
        output = dispatch_plan(action, dry_run=args.dry_run)
    except (KeyError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.format == "json":
        print(stable_report_json(output))
    else:
        if output["command"]:
            print(output["command"])
        else:
            print(f"{args.id}: no workflow dispatch is available")
    return 0


def cmd_fleetbot_render_command(args: argparse.Namespace) -> int:
    state = _command_center_state(args)
    try:
        output = render_command_response(
            command=args.command_name,
            state=state,
            repo=getattr(args, "repo", "") or "",
            run_id=getattr(args, "run_id", "") or "",
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.format == "json":
        print(stable_report_json(output))
    else:
        print(f"{output['title']}: {output.get('posture', output.get('empty', ''))}")
    return 0


def _command_center_state(args: argparse.Namespace) -> dict[str, Any]:
    input_path = getattr(args, "input", "") or ""
    if input_path:
        payload = json.loads(Path(input_path).read_text())
        state = _report_state_from_payload(payload)
        state = enrich_command_center_state(dict(state))
        return public_fleet_report_state(state)
    manifest = load_manifest(Path(getattr(args, "manifest", "fleet.yml")))
    report = dashboard_report(
        manifest,
        include_registry=getattr(args, "registry", False),
        include_activity=getattr(args, "include_activity", True),
        stale_days=getattr(args, "stale_days", 7),
        issue_repo=getattr(args, "issue_repo", "wgross19/aio-fleet"),
    )
    return public_fleet_report_state(dict(report["state"]))


def cmd_control_check(args: argparse.Namespace) -> int:
    if args.validation_only and args.publish_only:
        print(
            "--validation-only and --publish-only cannot be combined",
            file=sys.stderr,
        )
        return 1
    if (args.validation_only or args.publish_only) and not args.publish:
        print(
            "--validation-only and --publish-only require --publish",
            file=sys.stderr,
        )
        return 1
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    try:
        changed_paths = _changed_paths_from_json(
            str(getattr(args, "changed_paths_json", "") or "")
        )
        changed_files = _changed_files_from_json(
            str(getattr(args, "changed_files_json", "") or "")
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if getattr(args, "resolve_changed_files", False):
        changed_files = resolve_changed_files(
            repo,
            sha=args.sha,
            event=args.event,
            source=args.source,
        )
        if changed_files is None:
            print(
                "unable to resolve changed files for control-check scope",
                file=sys.stderr,
            )
            return 1
        changed_paths = _changed_paths_from_files(changed_files)
    if changed_paths is None and changed_files is not None:
        changed_paths = _changed_paths_from_files(changed_files)
    changed_file_statuses = _changed_file_statuses_from_files(changed_files)
    scope = (
        classify_required_check_scope(
            repo,
            changed_paths,
            changed_file_statuses=changed_file_statuses,
            publish=args.publish,
            fast_path_disabled=bool(getattr(args, "no_fast_path", False)),
        )
        if changed_paths is not None
        or changed_files is not None
        or getattr(args, "no_fast_path", False)
        else ChangeScope(CHECK_MODE_FULL)
    )
    if getattr(args, "fast_path_only", False) and (
        scope.check_mode != CHECK_MODE_FAST_CLEANUP
    ):
        failures = [
            "fast-path-scope: requested fast-cleanup but classified "
            f"{scope.check_mode}: {scope.fast_path_reason}"
        ]
        if args.report_json:
            report = _control_check_report(
                repo,
                sha=args.sha,
                event=args.event,
                source=args.source,
                publish=args.publish,
                publish_components=args.publish_component,
                failures=failures,
                transaction_id=str(getattr(args, "transaction_id", "") or ""),
                check_mode=scope.check_mode,
                changed_paths=scope.changed_paths,
                changed_files=changed_files or (),
                fast_path_reason=scope.fast_path_reason,
            )
            Path(args.report_json).write_text(
                json.dumps(report, indent=2, sort_keys=True)
            )
        print("\n".join(failures), file=sys.stderr)
        return 1
    if scope.check_mode == CHECK_MODE_FAST_CLEANUP:
        summary = "aio-fleet cleanup-only fast path passed"
        if scope.fast_path_reason:
            summary = f"{summary}: {scope.fast_path_reason}"
        if args.check_run:
            if args.dry_run:
                print(
                    json.dumps(
                        check_run_payload(
                            repo,
                            sha=args.sha,
                            event=args.event,
                            status="completed",
                            conclusion="success",
                            summary=summary,
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                upsert_check_run(
                    repo,
                    sha=args.sha,
                    event=args.event,
                    status="in_progress",
                    summary="aio-fleet central check started",
                )
                upsert_check_run(
                    repo,
                    sha=args.sha,
                    event=args.event,
                    status="completed",
                    conclusion="success",
                    summary=summary,
                )
        if args.report_json:
            report = _control_check_report(
                repo,
                sha=args.sha,
                event=args.event,
                source=args.source,
                publish=args.publish,
                publish_components=args.publish_component,
                failures=[],
                transaction_id=str(getattr(args, "transaction_id", "") or ""),
                check_mode=scope.check_mode,
                changed_paths=scope.changed_paths,
                changed_files=changed_files or (),
                fast_path_reason=scope.fast_path_reason,
            )
            Path(args.report_json).write_text(
                json.dumps(report, indent=2, sort_keys=True)
            )
        if not (args.check_run and args.dry_run):
            print(f"{repo.name}: check_mode=fast-cleanup {scope.fast_path_reason}")
        return 0
    steps = central_check_steps(
        repo,
        event=args.event,
        manifest_path=Path(args.manifest).resolve(),
        publish=args.publish,
        publish_component_names=args.publish_component,
        include_trunk=not args.no_trunk,
        include_integration=not args.no_integration,
        include_github_prereleases=not args.no_github_prereleases,
        include_app_checks=not args.publish_only,
        include_publish_steps=args.publish and not args.validation_only,
    )
    if args.check_run:
        status = "completed" if args.dry_run else "in_progress"
        summary = "aio-fleet central check started"
        if args.dry_run:
            print(
                json.dumps(
                    check_run_payload(
                        repo,
                        sha=args.sha,
                        event=args.event,
                        status=status,
                        conclusion="success" if status == "completed" else None,
                        summary="dry-run central check plan",
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            upsert_check_run(
                repo,
                sha=args.sha,
                event=args.event,
                status=status,
                summary=summary,
            )
    failures = run_steps(steps, dry_run=args.dry_run)
    if args.check_run and not args.dry_run:
        conclusion = "failure" if failures else "success"
        upsert_check_run(
            repo,
            sha=args.sha,
            event=args.event,
            status="completed",
            conclusion=conclusion,
            summary=(
                "\n".join(failures) if failures else "aio-fleet central check passed"
            ),
        )
    if args.report_json:
        report = _control_check_report(
            repo,
            sha=args.sha,
            event=args.event,
            source=args.source,
            publish=args.publish,
            publish_components=args.publish_component,
            failures=failures,
            transaction_id=str(getattr(args, "transaction_id", "") or ""),
            check_mode=scope.check_mode,
            changed_paths=scope.changed_paths,
            changed_files=changed_files or (),
            fast_path_reason=scope.fast_path_reason,
        )
        Path(args.report_json).write_text(json.dumps(report, indent=2, sort_keys=True))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


def cmd_workflow_control_report(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    try:
        changed_paths = _changed_paths_from_json(
            str(getattr(args, "changed_paths_json", "") or "")
        )
        changed_files = _changed_files_from_json(
            str(getattr(args, "changed_files_json", "") or "")
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if changed_paths is None and changed_files is not None:
        changed_paths = _changed_paths_from_files(changed_files)
    changed_file_statuses = _changed_file_statuses_from_files(changed_files)
    failures = list(args.failure or [])
    requested_check_mode = str(
        getattr(args, "check_mode", CHECK_MODE_FULL) or CHECK_MODE_FULL
    )
    check_mode = requested_check_mode
    report_changed_paths = tuple(changed_paths or ())
    fast_path_reason = str(getattr(args, "fast_path_reason", "") or "")
    fast_path_validation_failed = False
    if requested_check_mode == CHECK_MODE_FAST_CLEANUP:
        scope = classify_required_check_scope(
            repo,
            changed_paths,
            changed_file_statuses=changed_file_statuses,
            publish=args.publish,
        )
        check_mode = scope.check_mode
        report_changed_paths = scope.changed_paths
        fast_path_reason = scope.fast_path_reason
        if scope.check_mode != CHECK_MODE_FAST_CLEANUP:
            fast_path_validation_failed = True
            failures.append(
                "fast-path-scope: requested fast-cleanup but classified "
                f"{scope.check_mode}: {scope.fast_path_reason}"
            )
    report = _control_check_report(
        repo,
        sha=args.sha,
        event=args.event,
        source=args.source,
        publish=args.publish,
        publish_components=args.publish_component,
        failures=failures,
        transaction_id=str(getattr(args, "transaction_id", "") or ""),
        check_mode=check_mode,
        changed_paths=report_changed_paths,
        changed_files=changed_files or (),
        fast_path_reason=fast_path_reason,
    )
    if fast_path_validation_failed:
        report["status"] = "failure"
    elif args.status:
        report["status"] = args.status
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True))
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            "{repo}: control-report={status} classes={classes}".format(
                repo=repo.name,
                status=report["status"],
                classes=",".join(report.get("failure_classes", [])) or "none",
            )
        )
    return 1 if report.get("status") == "failure" else 0


def cmd_catalog_audit(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    findings = catalog_quality_findings(manifest, Path(args.catalog_path).resolve())
    report = {
        "catalog_path": str(Path(args.catalog_path).resolve()),
        "findings": findings,
        "summary": {"findings": len(findings)},
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.format == "markdown":
        print("# Catalog Audit")
        print()
        if findings:
            for finding in findings:
                print(f"- {finding}")
        else:
            print("No catalog audit findings.")
    else:
        if findings:
            print("\n".join(findings))
        else:
            print("catalog audit passed")
    return 1 if findings else 0


def cmd_catalog_changelog(args: argparse.Namespace) -> int:
    catalog_path = Path(args.catalog_path).resolve()
    rendered = render_catalog_changelog(catalog_path)
    drifted = catalog_changelog_drift(catalog_path)
    if args.write:
        (catalog_path / "CHANGELOG.md").write_text(rendered)
        drifted = False
    if args.check:
        if drifted:
            print(
                "catalog changelog drifted; run aio-fleet catalog-changelog --write",
                file=sys.stderr,
            )
            return 1
        print("catalog changelog is current")
        return 0
    if not args.write:
        print(rendered, end="")
    return 0


def cmd_catalog_workflow(args: argparse.Namespace) -> int:
    catalog_path = Path(args.catalog_path).resolve()
    aio_fleet_ref = args.aio_fleet_ref or current_aio_fleet_ref(
        Path(__file__).resolve().parents[2]
    )
    if args.write:
        write_validate_catalog_workflow(catalog_path, aio_fleet_ref=aio_fleet_ref)
    findings = catalog_workflow_findings(catalog_path, aio_fleet_ref=aio_fleet_ref)
    if args.check:
        if findings:
            print("\n".join(findings), file=sys.stderr)
            return 1
        print("catalog workflow is current")
        return 0
    if not args.write:
        print(render_validate_catalog_workflow(aio_fleet_ref), end="")
    return 0


def cmd_registry_verify(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    repos = (
        list(manifest.repos.values())
        if args.all
        else [_repo_for_identifier(manifest, args.repo)]
    )
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]
    failures: list[str] = []
    report: dict[str, object] = {"repos": []}
    for repo in repos:
        if args.all and repo.publish_profile == "template" and not args.include_manual:
            report["repos"].append(  # type: ignore[index]
                {
                    "repo": repo.name,
                    "sha": _git_head(repo.path),
                    "dockerhub": [],
                    "ghcr": [],
                    "failures": [],
                    "skipped": "manual-template-publish",
                }
            )
            continue
        sha = args.sha or _git_head(repo.path)
        components = publish_components(repo) if args.all else [args.component]
        for component in components:
            include_sha_tag = registry_sha_tag_required(
                repo, sha=sha, component=component
            )
            tags = compute_registry_tags(
                repo,
                sha=sha,
                component=component,
                include_sha_tag=include_sha_tag,
            )
            repo_failures = [] if args.dry_run else verify_registry_tags(tags.all_tags)
            failures.extend(
                f"{repo.name}:{component}: {failure}" for failure in repo_failures
            )
            report["repos"].append(  # type: ignore[index]
                {
                    "repo": repo.name,
                    "component": component,
                    "sha": sha,
                    "dockerhub": tags.dockerhub,
                    "ghcr": tags.ghcr,
                    "failures": repo_failures,
                    "sha_tag": "expected" if include_sha_tag else "skipped",
                }
            )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["repos"]:  # type: ignore[index]
            component = str(item.get("component", "aio"))  # type: ignore[union-attr]
            label = (
                str(item["repo"])  # type: ignore[index]
                if component == "aio"
                else f"{item['repo']}:{component}"  # type: ignore[index]
            )
            if item.get("skipped"):  # type: ignore[union-attr]
                print(
                    f"{label}: registry=skipped:{item['skipped']}"  # type: ignore[index]
                )
                continue
            state = "failed" if item["failures"] else "ok"  # type: ignore[index]
            print(f"{label}: registry={state}")
            if args.verbose or args.dry_run:
                for tag in [*item["dockerhub"], *item["ghcr"]]:  # type: ignore[index]
                    print(f"- {tag}")
        if failures:
            print("\n".join(failures), file=sys.stderr)
    return 1 if failures else 0


def cmd_registry_delete_dockerhub_tags(args: argparse.Namespace) -> int:
    tags = _tag_list_arg(args.tag_list, args.tag)
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    token = os.environ.get("DOCKERHUB_DELETE_TOKEN", "")
    try:
        image = _dockerhub_cleanup_image(args)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        results = delete_dockerhub_tags(
            image=image,
            tags=tags,
            username=username,
            token=token,
            required_substring=args.required_substring,
            dry_run=args.dry_run,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = {"image": image, "results": results}
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in results:
            print(f"{image}:{item['tag']}: {item['state']}")
    return 0


def _dockerhub_cleanup_image(args: argparse.Namespace) -> str:
    image = str(getattr(args, "image", "") or "").strip()
    repo_name = str(getattr(args, "repo", "") or "").strip()
    component = str(getattr(args, "component", "aio") or "aio").strip()
    if repo_name:
        manifest = load_manifest(Path(args.manifest))
        repo = _repo_for_identifier(manifest, repo_name)
        if getattr(args, "repo_path", None):
            repo = _repo_with_path(repo, Path(args.repo_path).resolve())
        resolved = str(component_config(repo, component).get("image_name") or "")
        if not resolved:
            resolved = repo.image_name
        if not resolved:
            raise ValueError(f"{repo.name}:{component}: no Docker Hub image configured")
        if image and image != resolved:
            raise ValueError(
                f"{image}: cleanup image does not match manifest target {resolved}"
            )
        return resolved
    if not image:
        raise ValueError("--image is required for dry-run cleanup without --repo")
    if not getattr(args, "dry_run", False):
        raise ValueError(
            "non-dry-run Docker Hub cleanup must target a manifest repo/component"
        )
    return image


def cmd_registry_preflight(args: argparse.Namespace) -> int:
    modes = set(args.mode or ["all"])
    if "all" in modes:
        modes = {"publish", "cleanup"}
    checks: list[dict[str, str]] = []

    manifest = load_manifest(Path(args.manifest))
    repo: RepoConfig | None = None
    if args.repo:
        repo = _repo_for_identifier(manifest, args.repo)
        if args.repo_path:
            repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    elif args.repo_path:
        print("--repo-path can only be used with --repo", file=sys.stderr)
        return 1

    image = args.image
    if not image and repo is not None:
        image = str(component_config(repo, args.component).get("image_name") or "")
        if not image:
            image = repo.image_name

    if "publish" in modes:
        checks.extend(_registry_publish_preflight_checks(live_auth=args.live_auth))
    if "cleanup" in modes:
        checks.extend(
            _registry_cleanup_preflight_checks(
                image=image,
                live_auth=args.live_auth,
                check_delete_scope=args.check_delete_scope,
                allow_publish_token_fallback=args.allow_publish_token_delete_fallback,
            )
        )

    report = {
        "status": (
            "failed" if any(check["status"] == "failed" for check in checks) else "ok"
        ),
        "checks": checks,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check['name']}: {check['status']}: {check['detail']}")
    return 1 if report["status"] == "failed" else 0


def _registry_publish_preflight_checks(*, live_auth: bool) -> list[dict[str, str]]:
    if os.environ.get("AIO_FLEET_REGISTRY_AUTH_MODE", "") == "preauthenticated":
        docker_config = os.environ.get("DOCKER_CONFIG", "")
        missing = [] if docker_config else ["DOCKER_CONFIG"]
        return [
            _preflight_check(
                "publish-credentials",
                "failed" if missing else "ok",
                (
                    "missing " + ", ".join(missing)
                    if missing
                    else "preauthenticated Docker config is present"
                ),
            ),
            _preflight_check(
                "dockerhub-publish-auth",
                "skipped" if not missing else "failed",
                (
                    "Docker Hub auth handled by the publish environment"
                    if not missing
                    else "preauthenticated Docker config is required"
                ),
            ),
        ]
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    dockerhub_token = os.environ.get("DOCKERHUB_TOKEN", "")
    ghcr_token = os.environ.get("AIO_FLEET_GHCR_TOKEN", "")
    missing = [
        name
        for name, value in (
            ("DOCKERHUB_USERNAME", username),
            ("DOCKERHUB_TOKEN", dockerhub_token),
            ("AIO_FLEET_GHCR_TOKEN", ghcr_token),
        )
        if not value
    ]
    checks = [
        _preflight_check(
            "publish-credentials",
            "failed" if missing else "ok",
            (
                "missing " + ", ".join(missing)
                if missing
                else "Docker Hub and GHCR publish credentials are present"
            ),
        )
    ]
    if missing:
        checks.append(
            _preflight_check(
                "dockerhub-publish-auth",
                "skipped",
                "publish credential gaps must be fixed before live auth",
            )
        )
        return checks
    if not live_auth:
        checks.append(
            _preflight_check(
                "dockerhub-publish-auth",
                "skipped",
                "live Docker Hub auth check disabled",
            )
        )
        return checks
    failure = dockerhub_auth_preflight_failure(
        username=username,
        token=dockerhub_token,
    )
    checks.append(
        _preflight_check(
            "dockerhub-publish-auth",
            "failed" if failure else "ok",
            failure or "Docker Hub token accepted by /v2/auth/token",
        )
    )
    return checks


def _registry_cleanup_preflight_checks(
    *,
    image: str,
    live_auth: bool,
    check_delete_scope: bool,
    allow_publish_token_fallback: bool,
) -> list[dict[str, str]]:
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    delete_token = os.environ.get("DOCKERHUB_DELETE_TOKEN", "")
    token = delete_token
    del allow_publish_token_fallback
    missing = []
    if not username:
        missing.append("DOCKERHUB_USERNAME")
    if not token:
        missing.append("DOCKERHUB_DELETE_TOKEN")
    checks = [
        _preflight_check(
            "cleanup-credentials",
            "failed" if missing else "ok",
            (
                "missing " + ", ".join(missing)
                if missing
                else "Docker Hub delete credentials are present"
            ),
        )
    ]
    if missing:
        checks.append(
            _preflight_check(
                "dockerhub-cleanup-auth",
                "skipped",
                "cleanup credential gaps must be fixed before live auth",
            )
        )
        return checks
    if not live_auth:
        checks.append(
            _preflight_check(
                "dockerhub-cleanup-auth",
                "skipped",
                "live Docker Hub auth check disabled",
            )
        )
        return checks
    failure = dockerhub_auth_preflight_failure(username=username, token=token)
    checks.append(
        _preflight_check(
            "dockerhub-cleanup-auth",
            "failed" if failure else "ok",
            failure or "Docker Hub delete token accepted by /v2/auth/token",
        )
    )
    if not check_delete_scope:
        return checks
    if not image:
        checks.append(
            _preflight_check(
                "dockerhub-delete-scope",
                "failed",
                "--image or --repo is required for delete-scope probing",
            )
        )
        return checks
    failure = dockerhub_delete_scope_preflight_failure(
        image=image,
        username=username,
        token=token,
    )
    checks.append(
        _preflight_check(
            "dockerhub-delete-scope",
            "failed" if failure else "ok",
            failure or "Docker Hub delete endpoint accepted a nonexistent-tag probe",
        )
    )
    return checks


def _preflight_check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def cmd_registry_publish(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    if repo.publish_profile == "template":
        print(
            f"{repo.name}: registry publish is disabled for template-profile repos",
            file=sys.stderr,
        )
        return 1
    sha = args.sha or _git_head(repo.path)
    command = registry_publish_command(repo, sha=sha, component=args.component)
    if args.dry_run:
        print(" ".join(shlex.quote(part) for part in command))
        return 0
    tags = compute_registry_tags(repo, sha=sha, component=args.component)
    if not getattr(args, "force", False):
        existing_failures = verify_registry_tags(tags.all_tags)
        if not existing_failures and _registry_tags_match_sha_digest(tags.all_tags):
            label = (
                repo.name
                if args.component == "aio"
                else f"{repo.name}:{args.component}"
            )
            print(f"{label}: registry=already-present")
            for tag in [*tags.dockerhub, *tags.ghcr]:
                print(f"- {tag}")
            return 0
    print(f"{repo.name}:{args.component}: registry=publishing", flush=True)
    try:
        with _registry_publish_environment(repo) as publish_env:
            preserved = _registry_preserve_tag_digests(
                _registry_preserve_tags(repo, args.component), env=publish_env
            )
            result = _run_streaming(command, cwd=repo.path, env=publish_env)
            if result.returncode != 0:
                return result.returncode
            verification_failures = verify_registry_tags(tags.all_tags, env=publish_env)
            verification_failures.extend(
                _registry_preserve_tag_failures(preserved, env=publish_env)
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    label = repo.name if args.component == "aio" else f"{repo.name}:{args.component}"
    state = "failed" if verification_failures else "ok"
    print(f"{label}: registry={state}")
    for tag in [*tags.dockerhub, *tags.ghcr]:
        print(f"- {tag}")
    if verification_failures:
        print(
            "\n".join(
                f"{repo.name}:{args.component}: {failure}"
                for failure in verification_failures
            ),
            file=sys.stderr,
        )
        return 1
    return 0


def _registry_tags_match_sha_digest(tags: list[str]) -> bool:
    sha_tags = [tag for tag in tags if ":sha-" in tag]
    if not sha_tags:
        return False
    for sha_tag in sha_tags:
        image = sha_tag.rsplit(":", 1)[0]
        expected = _registry_tag_digest(sha_tag, env=None)
        if not expected:
            return False
        related_tags = [tag for tag in tags if tag.rsplit(":", 1)[0] == image]
        for tag in related_tags:
            if _registry_tag_digest(tag, env=None) != expected:
                return False
    return True


def _tag_list_arg(value: str, repeated: list[str] | None) -> list[str]:
    tags: list[str] = []
    for item in repeated or []:
        tags.extend(part.strip() for part in item.replace(",", "\n").splitlines())
    tags.extend(part.strip() for part in value.replace(",", "\n").splitlines())
    return list(dict.fromkeys(tag for tag in tags if tag))


def _registry_preserve_tags(repo: RepoConfig, component: str) -> list[str]:
    value = component_config(repo, component).get("preserve_tags", [])
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    return [tag.strip() for tag in candidates if tag.strip()]


def _registry_preserve_tag_digests(
    tags: list[str], *, env: dict[str, str] | None
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for tag in tags:
        digest = _registry_tag_digest(tag, env=env)
        if not digest:
            raise RuntimeError(f"{tag}: unable to capture pre-publish digest")
        digests[tag] = digest
    return digests


def _registry_preserve_tag_failures(
    expected: dict[str, str], *, env: dict[str, str] | None
) -> list[str]:
    failures: list[str] = []
    for tag, before in expected.items():
        after = _registry_tag_digest(tag, env=env)
        if not after:
            failures.append(f"{tag}: unable to capture post-publish digest")
        elif after != before:
            failures.append(
                f"{tag}: protected digest changed during component publish "
                f"({before} -> {after})"
            )
    return failures


def _registry_tag_digest(tag: str, *, env: dict[str, str] | None) -> str:
    docker = shutil.which("docker")
    if docker is None:
        return ""
    result = subprocess.run(  # nosec B603
        [docker, "buildx", "imagetools", "inspect", tag],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Digest:"):
            return stripped.split(":", 1)[1].strip()
    return ""


@contextmanager
def _registry_publish_environment(repo: RepoConfig) -> Iterator[dict[str, str] | None]:
    preauthenticated = os.environ.get("AIO_FLEET_REGISTRY_AUTH_MODE", "")
    if preauthenticated == "preauthenticated":
        docker_config = os.environ.get("DOCKER_CONFIG", "")
        if not docker_config:
            raise RuntimeError(
                "preauthenticated registry publish requires DOCKER_CONFIG"
            )
        publish_env = {
            key: value
            for key, value in os.environ.items()
            if not _secret_environment_key(key)
        }
        publish_env["DOCKER_CONFIG"] = docker_config
        builder_name = f"aio-fleet-{repo.name}-{uuid.uuid4().hex[:12]}"
        try:
            _create_buildx_builder(builder_name, env=publish_env)
            publish_env["BUILDX_BUILDER"] = builder_name
            yield publish_env
        finally:
            _remove_buildx_builder(builder_name, env=publish_env)
        return

    dockerhub_username = os.environ.get("DOCKERHUB_USERNAME", "")
    dockerhub_token = os.environ.get("DOCKERHUB_TOKEN", "")
    ghcr_token = os.environ.get("AIO_FLEET_GHCR_TOKEN", "")
    configured = any([dockerhub_username, dockerhub_token, ghcr_token])
    if not configured:
        yield None
        return

    missing = []
    if not dockerhub_username:
        missing.append("DOCKERHUB_USERNAME")
    if not dockerhub_token:
        missing.append("DOCKERHUB_TOKEN")
    if not ghcr_token:
        missing.append("AIO_FLEET_GHCR_TOKEN")
    if missing:
        raise RuntimeError(
            "registry publish credentials are incomplete: " + ", ".join(missing)
        )

    ghcr_username = (
        os.environ.get("AIO_FLEET_GHCR_USERNAME")
        or os.environ.get("GITHUB_REPOSITORY_OWNER")
        or repo.owner
    )
    with tempfile.TemporaryDirectory(prefix="aio-fleet-docker-") as docker_config:
        publish_env = {
            key: value
            for key, value in os.environ.items()
            if not _secret_environment_key(key)
        }
        publish_env["DOCKER_CONFIG"] = docker_config
        builder_name = f"aio-fleet-{repo.name}-{uuid.uuid4().hex[:12]}"
        try:
            _docker_login(
                "docker.io",
                username=dockerhub_username,
                token=dockerhub_token,
                env=publish_env,
            )
            _docker_login(
                "ghcr.io",
                username=ghcr_username,
                token=ghcr_token,
                env=publish_env,
            )
            _create_buildx_builder(builder_name, env=publish_env)
            publish_env["BUILDX_BUILDER"] = builder_name
            yield publish_env
        finally:
            _remove_buildx_builder(builder_name, env=publish_env)


def _docker_login(
    registry: str, *, username: str, token: str, env: dict[str, str]
) -> None:
    result = subprocess.run(  # nosec B603 B607
        [
            "docker",
            "login",
            registry,
            "--username",
            username,
            "--password-stdin",
        ],
        input=f"{token}\n",
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"docker login failed for {registry}: {detail}")


def _create_buildx_builder(name: str, *, env: dict[str, str]) -> None:
    create = subprocess.run(  # nosec B603 B607
        [
            "docker",
            "buildx",
            "create",
            "--name",
            name,
            "--driver",
            "docker-container",
            "--use",
        ],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if create.returncode != 0:
        detail = (create.stderr or create.stdout).strip()
        raise RuntimeError(f"docker buildx builder creation failed: {detail}")
    inspect = subprocess.run(  # nosec B603 B607
        ["docker", "buildx", "inspect", "--bootstrap", name],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if inspect.returncode != 0:
        detail = (inspect.stderr or inspect.stdout).strip()
        raise RuntimeError(f"docker buildx builder bootstrap failed: {detail}")


def _remove_buildx_builder(name: str, *, env: dict[str, str]) -> None:
    subprocess.run(  # nosec B603 B607
        ["docker", "buildx", "rm", "--force", name],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def cmd_upstream_monitor(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    repos = (
        list(manifest.repos.values())
        if args.all
        else [_repo_for_identifier(manifest, args.repo)]
    )
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]

    report: dict[str, object] = {"repos": []}
    failed = False
    for repo in repos:
        if repo.publish_profile == "template" and not args.include_manual:
            report["repos"].append(  # type: ignore[index]
                {"repo": repo.name, "skipped": "manual-template"}
            )
            continue
        try:
            results = monitor_repo(repo, write=args.write and not args.dry_run)
            writeable_updates = [
                result
                for result in results
                if result.updates_available
                and result.strategy == "pr"
                and not getattr(result, "blocked", False)
            ]
            if args.write and not args.dry_run and writeable_updates:
                _run_generator_for_write(repo)
            actions: list[dict[str, object]] = []
            blocked = [
                result for result in results if getattr(result, "blocked", False)
            ]
            if args.create_pr and blocked:
                actions.append(
                    {
                        "repo": repo.name,
                        "action": "skipped",
                        "reason": "blocked-upstream-update",
                        "blockers": [result_dict(result) for result in blocked],
                    }
                )
            if args.create_pr and writeable_updates:
                actions.append(
                    create_or_update_upstream_pr(
                        repo,
                        results,
                        dry_run=args.dry_run,
                        post_check=args.post_check,
                    )
                )
            report["repos"].append(  # type: ignore[index]
                {
                    "repo": repo.name,
                    "results": [result_dict(result) for result in results],
                    "actions": actions,
                }
            )
        except Exception as exc:
            failed = True
            report["repos"].append(  # type: ignore[index]
                {"repo": repo.name, "error": str(exc)}
            )

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["repos"]:  # type: ignore[index]
            if item.get("skipped"):  # type: ignore[union-attr]
                print(f"{item['repo']}: upstream=skipped:{item['skipped']}")  # type: ignore[index]
                continue
            if item.get("error"):  # type: ignore[union-attr]
                print(f"{item['repo']}: upstream=failed:{item['error']}")  # type: ignore[index]
                continue
            results = item["results"]  # type: ignore[index]
            updates = [
                result
                for result in results
                if result["updates_available"]  # type: ignore[index]
            ]
            blocked = [
                result
                for result in results
                if result.get("state") == "blocked"  # type: ignore[union-attr]
            ]
            state = "blocked" if blocked else "updates" if updates else "ok"
            print(f"{item['repo']}: upstream={state}")  # type: ignore[index]
            for result in results:
                print(
                    "- {component}: {current_version} -> {latest_version} "
                    "version_update={version_update} digest_update={digest_update}".format(
                        **result
                    )
                )
                if result.get("state") == "blocked":  # type: ignore[union-attr]
                    print(
                        "  blocked: {blocked_reason}; next={next_action}".format(
                            **result
                        )
                    )
    return 1 if failed else 0


def cmd_upstream_assess(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    if args.pr or args.branch:
        assessment = assess_upstream_pr(
            repo,
            pr_number=args.pr,
            branch=args.branch,
        )
    else:
        assessment = assess_expected_update(
            repo,
            monitor_repo(repo, write=False),
            changed_files=[],
        )
    payload = assessment.to_dict()
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "{repo}: safety={safety_level} confidence={confidence:.2f} next={next_action}".format(
                **payload
            )
        )
        for failure in payload["failures"]:
            print(f"- blocking: {failure}")
        for warning in payload["warnings"]:
            print(f"- warning: {warning}")
    return 1 if assessment.safety_level == "blocked" else 0


def _run_generator_for_write(repo: RepoConfig) -> None:
    if APP_MANIFEST_NAME in repo.list_value("upstream_commit_paths"):
        (repo.path / APP_MANIFEST_NAME).write_text(render_app_manifest(repo))
    generator = str(repo.get("generator_check_command", "") or "").strip()
    if not generator:
        return
    command = [part for part in shlex.split(generator) if part != "--check"]
    safe_env = {
        key: value
        for key, value in os.environ.items()
        if not _secret_environment_key(key)
    }
    safe_env["HOME"] = tempfile.mkdtemp(prefix="aio-fleet-generator-home-")
    result = _run(command, cwd=repo.path, env=safe_env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{repo.name}: generator update failed: {detail}")


def cmd_release_readiness(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    component = str(getattr(args, "component", "aio") or "aio")
    catalog_path = Path(args.catalog_path).resolve() if args.catalog_path else None
    policy_path = Path(args.policy)
    findings: list[str] = []
    warnings: list[str] = []
    label = repo.name if component == "aio" else f"{repo.name}:{component}"

    status = _run(["git", "status", "--short"], cwd=repo.path)
    if status.stdout.strip():
        findings.append(f"{label}: worktree is dirty")

    drift = _run(
        ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
        cwd=repo.path,
    )
    ahead = behind = "?"
    if drift.returncode == 0:
        ahead, behind = (drift.stdout.strip().split() + ["0", "0"])[:2]
        if behind != "0":
            findings.append(f"{label}: branch is behind origin/main by {behind}")
    else:
        warnings.append(f"{label}: unable to inspect branch drift")

    open_prs = _open_prs(repo)
    if open_prs not in {"0", "not-run", "unknown"}:
        findings.append(f"{label}: has {open_prs} open PR(s)")

    try:
        policy_repos = set(load_policy(policy_path)["repositories"])
        if repo.name in policy_repos:
            findings.extend(
                validate_github_policy(
                    policy_path, repos=[repo.name], check_secrets=False
                )
            )
        else:
            warnings.append(f"{repo.name}: github policy is manual")
    except Exception as exc:
        warnings.append(f"{label}: unable to validate GitHub policy: {exc}")

    if catalog_path:
        catalog_failures = [
            failure
            for failure in catalog_repo_failures(manifest, catalog_path)
            if failure.startswith(f"{repo.name}:")
        ]
        findings.extend(catalog_failures)

    latest_ci = _latest_main_ci(repo)
    if latest_ci["state"] != "success":
        findings.append(f"{label}: latest main CI is {latest_ci['state']}")

    release_version = _release_version(repo, component=component)
    if not release_version:
        findings.append(f"{label}: unable to read latest changelog version")

    image_status = _image_status(repo, component=component)
    if image_status != "ok":
        warnings.append(f"{label}: image publish status is {image_status}")

    sha = _git_head(repo.path)
    operator_commands = {
        "registry_verify": fleet_command(
            "registry",
            "verify",
            "--repo",
            repo.name,
            "--component",
            component,
            "--sha",
            sha or "<sha>",
            "--verbose",
        ),
        "registry_publish": fleet_command(
            "registry", "publish", "--repo", repo.name, "--component", component
        ),
        "release_publish": fleet_command(
            "release", "publish", "--repo", repo.name, "--component", component
        ),
        "control_check_publish": control_check_publish_command(
            repo, component=component, sha=sha
        ),
    }

    report = {
        "repo": repo.name,
        "component": component,
        "ahead": ahead,
        "behind": behind,
        "open_prs": open_prs,
        "latest_ci": latest_ci,
        "release_version": release_version,
        "image_status": image_status,
        "findings": findings,
        "warnings": warnings,
        "operator_commands": operator_commands,
        "ready": not findings,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        state = "ready" if not findings else "blocked"
        print(f"{label}: release-readiness={state}")
        for finding in findings:
            print(f"- {finding}")
        for warning in warnings:
            print(f"- warning: {warning}")
        print("- next verify: " + operator_commands["registry_verify"])
        print("- next publish: " + operator_commands["control_check_publish"])
        print("- next release: " + operator_commands["release_publish"])
    return 1 if findings else 0


def _latest_main_ci(repo: RepoConfig) -> dict[str, str]:
    sha_result = _run(
        ["gh", "api", f"repos/{repo.github_repo}/commits/main", "--jq", ".sha"],
        cwd=repo.path,
    )
    if sha_result.returncode != 0:
        return {"state": "unknown", "detail": sha_result.stderr.strip()}
    sha = sha_result.stdout.strip()
    result = _run(
        [
            "gh",
            "api",
            f"repos/{repo.github_repo}/commits/{sha}/check-runs?check_name=aio-fleet%20%2F%20required",
        ],
        cwd=repo.path,
    )
    if result.returncode != 0:
        return {"state": "unknown", "detail": result.stderr.strip()}
    try:
        runs = json.loads(result.stdout or "{}").get("check_runs", [])
    except json.JSONDecodeError:
        return {"state": "unknown", "detail": "unable to parse gh run output"}
    if not runs:
        return {
            "state": "missing",
            "head_sha": sha,
            "detail": f"no {CHECK_NAME} check found",
        }
    external_id = check_external_id(repo, sha=sha, event="push")
    run = next(
        (
            item
            for item in runs
            if isinstance(item, dict) and item.get("external_id") == external_id
        ),
        None,
    )
    if run is None:
        return {
            "state": "missing",
            "head_sha": sha,
            "detail": f"no externally-bound {CHECK_NAME} check found",
        }
    state = (
        "success"
        if run.get("status") == "completed" and run.get("conclusion") == "success"
        else str(run.get("conclusion") or run.get("status") or "unknown")
    )
    return {
        "state": state,
        "head_sha": sha,
        "url": str(run.get("html_url") or ""),
    }


def _release_version(repo: RepoConfig, *, component: str = "aio") -> str:
    try:
        config = component_config(repo, component)
        changelog = repo.path / str(config.get("release_changelog", "CHANGELOG.md"))
        return latest_changelog_version(
            changelog, semver=repo.publish_profile == "template"
        )
    except (Exception, SystemExit):
        return ""


def _image_status(repo: RepoConfig, *, component: str = "aio") -> str:
    docker = shutil.which("docker")
    if docker is None:
        return "unknown:no-docker"
    image_name = str(
        component_config(repo, component).get("image_name", repo.image_name)
    )
    floating_tags = component_config(repo, component).get("floating_tags", ["latest"])
    tag = (
        str(floating_tags[0])
        if isinstance(floating_tags, list) and floating_tags
        else "latest"
    )
    result = _run(
        [docker, "manifest", "inspect", f"{image_name}:{tag}"],
        cwd=repo.path,
    )
    return "ok" if result.returncode == 0 else "unknown:latest-not-inspected"


def cmd_release_status(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    plan = build_release_plan(repo, component=args.component)
    tags = compute_registry_tags(
        repo, sha=_git_head(repo.path), component=args.component
    )
    report = {
        "repo": repo.name,
        "component": plan.component,
        "version": plan.version,
        "changelog": str(plan.changelog_path),
        "xml_paths": [str(path) for path in plan.xml_paths],
        "registry_tags": tags.all_tags,
    }
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        label = (
            repo.name if plan.component == "aio" else f"{repo.name}:{plan.component}"
        )
        print(f"{label}: next_release={plan.version}")
        print(f"changelog: {plan.changelog_path}")
        for path in plan.xml_paths:
            print(f"xml: {path}")
    return 0


def cmd_release_plan(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    if args.all:
        plans = release_plan_for_manifest(
            manifest,
            include_registry=args.registry,
            redact_private=True,
            registry_verify_attempts=args.registry_verify_attempts,
        )
    else:
        repo = _repo_for_identifier(manifest, args.repo)
        if args.repo_path:
            repo = _repo_with_path(repo, Path(args.repo_path).resolve())
        if args.component:
            plans = [
                release_plan_for_repo(
                    repo,
                    include_registry=args.registry,
                    component=args.component,
                    registry_verify_attempts=args.registry_verify_attempts,
                )
            ]
        else:
            plans = release_plan_rows_for_repo(
                repo,
                include_registry=args.registry,
                registry_verify_attempts=args.registry_verify_attempts,
            )
    report = {
        "repos": plans,
        "summary": {
            "repos": len(plans),
            "release_due": len(
                [
                    plan
                    for plan in plans
                    if plan.get("state")
                    in {"release-due", "catalog-sync-needed", "publish-missing"}
                ]
            ),
            "publish_missing": len(
                [plan for plan in plans if plan.get("state") == "publish-missing"]
            ),
            "sha_tag_missing": len(
                [
                    plan
                    for plan in plans
                    if plan.get("registry_state") == "sha-tag-missing"
                ]
            ),
        },
    }
    if args.format == "json":
        print(stable_report_json(report))
    else:
        for plan in plans:
            label = (
                plan["repo"]
                if plan.get("component", "aio") == "aio"
                else f"{plan['repo']}:{plan.get('component', 'aio')}"
            )
            print(
                "{label}: release={state} next={next_version} action={next_action}".format(
                    label=label, **plan
                )
            )
    return 1 if report["summary"]["publish_missing"] else 0


def cmd_release_reconcile(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    rows = _release_reconcile_rows(args, manifest)
    actions = _release_reconcile_actions(rows)
    if args.create_upstream_prs:
        actions.extend(_release_reconcile_upstream_actions(args, manifest))
    report = {
        "status": "actionable" if actions else "ok",
        "actions": actions,
        "summary": {
            "actions": len(actions),
            "publish": len(
                [action for action in actions if action["action"] == "publish"]
            ),
            "source_pr": len(
                [action for action in actions if action["action"] == "source-pr"]
            ),
            "catalog_sync": len(
                [action for action in actions if action["action"] == "catalog-sync"]
            ),
        },
    }
    if args.format == "json":
        print(stable_report_json(report))
    else:
        if not actions:
            print("release queue: no actionable work")
        for action in actions:
            label = action["repo"]
            if action.get("component") and action["component"] != "aio":
                label = f"{label}:{action['component']}"
            print(f"{label}: {action['action']} {action.get('command', '')}".strip())
    return 0


def cmd_release_preflight(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    components = [args.component] if args.component else None
    report = release_transaction_preflight(
        repo,
        components=components,
        expected_sha=str(args.sha or ""),
        event=args.event,
        write=args.write,
        require_credentials=args.require_credentials,
        required_checks_passed=args.required_checks_passed,
        mode=args.mode,
    )
    if args.format == "json":
        print(stable_report_json(report))
    else:
        label = repo.name
        if args.component:
            label = f"{label}:{args.component}"
        print(f"{label}: release-preflight={report['status']}")
        for finding in report["findings"]:
            print(f"- {finding['message']}")
        for warning in report["warnings"]:
            print(f"- warning: {warning['message']}")
    return 1 if report["status"] == "blocked" else 0


def cmd_release_transaction(args: argparse.Namespace) -> int:
    if getattr(args, "transaction_command", "") == "resume":
        return cmd_release_transaction_resume(args)
    if not args.repo:
        print("--repo is required unless transaction resume is used", file=sys.stderr)
        return 1
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    components = [args.component] if args.component else None
    dry_run = args.dry_run or not args.write
    report = release_transaction_report(
        repo,
        components=components,
        expected_sha=str(args.sha or ""),
        event=args.event,
        write=args.write,
        dry_run=dry_run,
        transaction_id=str(args.transaction_id or ""),
        require_credentials=args.require_credentials,
        required_checks_passed=args.required_checks_passed,
    )
    if args.report_json:
        Path(args.report_json).write_text(stable_report_json(report) + "\n")
    if args.format == "json":
        print(stable_report_json(report))
    else:
        label = repo.name
        if args.component:
            label = f"{label}:{args.component}"
        print(
            f"{label}: release-transaction={report['status']} "
            f"id={report['transaction_id']}"
        )
        for finding in report["preflight"]["findings"]:
            print(f"- {finding['message']}")
        commands = report.get("operator_commands", {}).get("transaction", [])
        for command in commands:
            print(f"- next: {command}")
    return 1 if report["status"] == "blocked" else 0


def cmd_release_transaction_resume(args: argparse.Namespace) -> int:
    report = release_transaction_resume_report(args.id)
    if args.format == "json":
        print(stable_report_json(report))
    else:
        print(f"{args.id}: release-transaction={report['status']}")
        for finding in report["findings"]:
            print(f"- {finding['message']}")
    return 1


def _release_reconcile_rows(
    args: argparse.Namespace, manifest: FleetManifest
) -> list[dict[str, object]]:
    if args.input:
        payload = json.loads(Path(args.input).read_text())
        state = _report_state_from_payload(payload)
        if isinstance(state.get("repos"), list):
            rows = [row for row in state["repos"] if isinstance(row, dict)]
        elif isinstance(state.get("releases"), list):
            rows = [row for row in state["releases"] if isinstance(row, dict)]
        else:
            raise ManifestError(
                "release reconcile input must contain repos or releases"
            )
    elif args.all:
        rows = release_plan_for_manifest(manifest, include_registry=args.registry)
    else:
        if not args.repo:
            raise ManifestError("--repo is required unless --all or --input is used")
        repo = _repo_for_identifier(manifest, args.repo)
        if args.repo_path:
            repo = _repo_with_path(repo, Path(args.repo_path).resolve())
        rows = (
            [
                release_plan_for_repo(
                    repo,
                    include_registry=args.registry,
                    component=args.component,
                )
            ]
            if args.component
            else release_plan_rows_for_repo(repo, include_registry=args.registry)
        )
    if args.repo:
        rows = [row for row in rows if row.get("repo") == args.repo]
    if args.component:
        rows = [row for row in rows if row.get("component", "aio") == args.component]
    return rows


def _release_reconcile_actions(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    for row in rows:
        state = str(row.get("state", ""))
        repo = str(row.get("repo", ""))
        component = str(row.get("component", "aio") or "aio")
        command = str(row.get("next_action", "") or "")
        if not repo or state in {"", "current", "private-skipped", "watch"}:
            continue
        if state == "publish-missing":
            actions.append(
                {
                    "repo": repo,
                    "component": component,
                    "state": state,
                    "action": "publish",
                    "command": command,
                }
            )
        elif state == "catalog-sync-needed":
            actions.append(
                {
                    "repo": repo,
                    "component": component,
                    "state": state,
                    "action": "catalog-sync",
                    "command": command,
                }
            )
        elif state == "release-due":
            actions.append(
                {
                    "repo": repo,
                    "component": component,
                    "state": state,
                    "action": "source-pr",
                    "command": command,
                }
            )
    return actions


def _release_reconcile_upstream_actions(
    args: argparse.Namespace, manifest: FleetManifest
) -> list[dict[str, object]]:
    selected = [args.repo] if args.repo else list(manifest.repos.keys())
    actions: list[dict[str, object]] = []
    for name in selected:
        repo = _repo_for_identifier(manifest, name)
        if repo.publish_profile == "template":
            continue
        results = monitor_repo(repo, write=args.write and not args.dry_run)
        writeable_updates = [
            result
            for result in results
            if result.updates_available
            and result.strategy == "pr"
            and not getattr(result, "blocked", False)
        ]
        if not writeable_updates:
            continue
        if args.write and not args.dry_run:
            _run_generator_for_write(repo)
        action = create_or_update_upstream_pr(
            repo,
            results,
            dry_run=args.dry_run or not args.write,
            post_check=args.post_check,
        )
        action["action"] = "source-pr"
        actions.append(action)
    return actions


def cmd_release_prepare(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    plan = build_release_plan(repo, component=args.component)
    # Emit the authoritative prepared version so the dispatch workflow does not
    # have to guess it by grepping CHANGELOG.md, which is wrong for component
    # lanes that maintain their own changelog (e.g. the alpha lane's
    # CHANGELOG.alpha.md).
    if getattr(args, "github_output", None):
        with Path(args.github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"version={plan.version}\n")
    cliff_config = write_temp_git_cliff_config(
        repo,
        release_suffix=plan.release_suffix,
        release_tag_prefix=plan.release_tag_prefix,
    )
    commands = []
    if not release_section_exists(plan.version, plan.changelog_path):
        commands.append(
            [
                "git",
                "cliff",
                "--config",
                str(cliff_config),
                "--tag",
                plan.version,
                "--unreleased",
                "--prepend",
                str(plan.changelog_path),
            ]
        )
    if args.dry_run:
        print(f"{repo.name}: would prepare release {plan.version}")
        if commands:
            for command in commands:
                print(" ".join(shlex.quote(part) for part in command))
        else:
            print(f"would reuse existing release section in {plan.changelog_path}")
        if plan.registry_revision_path is not None:
            print(
                "would update "
                f"ARG {plan.registry_revision_arg}={plan.registry_revision_value} "
                f"in {plan.registry_revision_path}"
            )
        for xml_path in plan.xml_paths:
            print(f"would update <Changes> in {xml_path}")
        return 0
    for command in commands:
        result = _run(command, cwd=repo.path)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode != 0:
            return result.returncode
    normalize_markdown_changelog(plan.changelog_path)
    if update_registry_revision_arg(plan):
        print(
            "updated "
            f"ARG {plan.registry_revision_arg}={plan.registry_revision_value} "
            f"in {plan.registry_revision_path}"
        )
    for xml_path in plan.xml_paths:
        update_template_changes(
            version=plan.version,
            changelog=plan.changelog_path,
            template=xml_path,
        )
        print(f"updated {xml_path}")
    return 0


def cmd_release_publish_github_prereleases(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    components = args.component or publish_components(repo)
    failures: list[str] = []
    guard_failures = _github_prerelease_publish_guard_failures(
        repo,
        components=components,
        expected_sha=str(getattr(args, "expected_sha", "") or ""),
        control_report_json=str(getattr(args, "control_report_json", "") or ""),
        require_token=not args.dry_run,
    )
    if guard_failures:
        failures.extend(guard_failures)
        if args.control_report_json:
            _append_control_report_failures(Path(args.control_report_json), failures)
        print("\n".join(failures), file=sys.stderr)
        return 1
    published = 0
    for component in components:
        config = component_config(repo, component)
        if str(config.get("release_history", "")).strip() != "github_prerelease":
            continue
        try:
            report = _publish_github_prerelease(
                repo, component=component, dry_run=args.dry_run
            )
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            failures.append(f"github-prerelease-{component}: exit {code}")
            continue
        published += 1
        print("{repo}:{component}: prerelease={action} {tag}".format(**report))
    if args.control_report_json and failures:
        _append_control_report_failures(Path(args.control_report_json), failures)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    if published == 0:
        print(f"{repo.name}: no GitHub prereleases to publish")
    return 0


def _append_control_report_failures(path: Path, failures: list[str]) -> None:
    try:
        report = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    existing = report.get("failures")
    if not isinstance(existing, list):
        existing = []
    existing.extend(failures)
    report["failures"] = existing
    report["failure_classes"] = _failure_classes(existing)
    report["status"] = "failure"
    path.write_text(json.dumps(report, indent=2, sort_keys=True))


def _github_prerelease_publish_guard_failures(
    repo: RepoConfig,
    *,
    components: list[str],
    expected_sha: str,
    control_report_json: str,
    require_token: bool,
) -> list[str]:
    failures: list[str] = []
    report: dict[str, object] = {}
    if control_report_json:
        try:
            report = json.loads(Path(control_report_json).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(
                f"checkout-mismatch: unable to read control report before prerelease publish: {exc}"
            )
        else:
            failures.extend(
                _control_report_publish_guard_failures(report, repo.name, components)
            )
            expected_sha = expected_sha or _control_report_expected_sha(report)
    if not expected_sha:
        failures.append(
            "checkout-mismatch: expected SHA is required before prerelease publish"
        )
    elif not FULL_SHA_RE.fullmatch(expected_sha):
        failures.append(
            f"checkout-mismatch: expected SHA must be a full commit SHA, got {expected_sha}"
        )

    head = _git_head(repo.path)
    if expected_sha and FULL_SHA_RE.fullmatch(expected_sha) and head != expected_sha:
        failures.append(
            f"checkout-mismatch: app checkout HEAD {head or '<unknown>'} does not match expected {expected_sha}"
        )

    status = _run(["git", "status", "--short"], cwd=repo.path)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip()
        failures.append(
            f"checkout-mismatch: unable to inspect app checkout cleanliness: {detail or 'git status failed'}"
        )
    elif status.stdout.strip():
        failures.append(
            "checkout-mismatch: app checkout is dirty before release publish"
        )

    if require_token and _github_cli_env() is None:
        failures.append(
            "credential-gap: missing AIO_FLEET_RELEASE_TOKEN, GH_TOKEN, or GITHUB_TOKEN for GitHub prerelease publish"
        )
    return failures


def _control_report_publish_guard_failures(
    report: dict[str, object], expected_repo: str, components: list[str]
) -> list[str]:
    failures: list[str] = []
    report_repo = str(report.get("repo", "") or "").strip()
    if report_repo != expected_repo:
        failures.append(
            f"control-report: repo is {report_repo or '<missing>'}, expected {expected_repo}"
        )
    if report.get("status") != "success":
        failures.append(
            f"control-report: central control-check status is {report.get('status', '<missing>')}"
        )
    if report.get("publish") is not True:
        failures.append("control-report: publish was not authorized by control-check")

    attested_components = _control_report_publish_components(report)
    missing = sorted(set(components) - attested_components)
    if missing:
        failures.append(
            "control-report: component(s) not attested for publish: "
            + ", ".join(missing)
        )
    return failures


def _control_report_expected_sha(report: dict[str, object]) -> str:
    attestation = report.get("publish_attestation", {})
    if isinstance(attestation, dict):
        expected = str(attestation.get("expected_sha", "") or "").strip()
        if expected:
            return expected
    return str(report.get("sha", "") or "").strip()


def _control_report_publish_components(report: dict[str, object]) -> set[str]:
    attestation = report.get("publish_attestation", {})
    if isinstance(attestation, dict):
        components = attestation.get("publish_components", [])
        if isinstance(components, list):
            return {str(component) for component in components if str(component)}
    components = report.get("components", [])
    if isinstance(components, list):
        return {
            str(component.get("component", ""))
            for component in components
            if isinstance(component, dict) and str(component.get("component", ""))
        }
    return set()


def cmd_release_publish(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    if args.repo_path:
        repo = _repo_with_path(repo, Path(args.repo_path).resolve())
    config = component_config(repo, args.component)
    if str(config.get("release_history", "")).strip() == "github_prerelease":
        report = _publish_github_prerelease(
            repo, component=args.component, dry_run=args.dry_run
        )
        if args.report_json:
            Path(args.report_json).write_text(
                json.dumps(report, indent=2, sort_keys=True)
            )
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print("{repo}:{component}: prerelease={action} {tag}".format(**report))
        return 0
    if str(config.get("release_policy", "")).strip() == "registry_only":
        report = {
            "repo": repo.name,
            "component": args.component,
            "status": "skipped",
            "reason": "registry_only",
        }
        if args.report_json:
            Path(args.report_json).write_text(
                json.dumps(report, indent=2, sort_keys=True)
            )
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(f"{repo.name}:{args.component}: github-release=skipped registry_only")
        return 0
    latest_version = _component_release_version(repo, component=args.component)
    release_tag = _github_release_tag(repo, latest_version, component=args.component)
    release_target = find_release_publish_target_commit(repo.path, latest_version)
    notes = _run(
        [
            sys.executable,
            "-m",
            "aio_fleet.release",
            "--repo-path",
            str(repo.path),
            "--release-profile",
            "semver" if repo.publish_profile == "template" else "aio",
            "extract-release-notes",
            latest_version,
        ],
        cwd=repo.path,
    )
    if notes.returncode != 0:
        print(notes.stderr or notes.stdout, file=sys.stderr, end="")
        return notes.returncode
    body = notes.stdout.strip()
    create_command = [
        "gh",
        "release",
        "create",
        release_tag,
        "--repo",
        repo.github_repo,
        "--target",
        release_target,
        "--title",
        release_tag,
        "--notes",
        body,
    ]
    if args.dry_run:
        print(" ".join(shlex.quote(part) for part in create_command))
        return 0
    env = _github_cli_env()
    if env is None:
        print(
            "credential-gap: missing AIO_FLEET_RELEASE_TOKEN, GH_TOKEN, or "
            "GITHUB_TOKEN for GitHub release publish",
            file=sys.stderr,
        )
        return 1
    # Idempotent per the dispatch-driven release contract: edit an existing
    # release in place, otherwise create it, so a publish dispatch that already
    # pushed registry tags (or a retried run) still completes the GitHub release
    # instead of failing on "release already exists".
    already_published = (
        _run(
            ["gh", "release", "view", release_tag, "--repo", repo.github_repo],
            cwd=repo.path,
            env=env,
        ).returncode
        == 0
    )
    if already_published:
        command = [
            "gh",
            "release",
            "edit",
            release_tag,
            "--repo",
            repo.github_repo,
            "--title",
            release_tag,
            "--notes",
            body,
        ]
    else:
        command = create_command
    result = _run(command, cwd=repo.path, env=env)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if args.report_json:
        report = {
            "repo": repo.name,
            "component": args.component,
            "status": "success" if result.returncode == 0 else "failure",
            "tag": release_tag,
            "target": release_target,
            "url": _github_release_url(repo, release_tag),
        }
        Path(args.report_json).write_text(json.dumps(report, indent=2, sort_keys=True))
    return result.returncode


def _github_release_tag(
    repo: RepoConfig, version: str, *, component: str = "aio"
) -> str:
    if version.startswith("v") or repo.publish_profile == "template":
        return version
    config = component_config(repo, component)
    suffix = str(config.get("release_suffix", "aio"))
    latest_tag = latest_component_release_tag(repo.path, suffix)
    if latest_tag and latest_tag.startswith("v"):
        return f"v{version}"
    return version


def _component_release_version(repo: RepoConfig, *, component: str = "aio") -> str:
    config = component_config(repo, component)
    changelog = repo.path / str(config.get("release_changelog", "CHANGELOG.md"))
    if repo.publish_profile == "template":
        return latest_changelog_version(changelog, semver=True)
    if repo.publish_profile == "changelog-version":
        return latest_changelog_version(changelog)
    upstream_version = read_upstream_version(
        repo.path / str(config.get("dockerfile", "Dockerfile")),
        repo.path / str(config.get("upstream_config", "upstream.toml")),
        version_key=str(config.get("upstream_version_key", "UPSTREAM_VERSION")),
    )
    return latest_component_changelog_version(
        changelog,
        upstream_version=upstream_version,
        suffix=str(config.get("release_suffix", "aio")),
    )


def _control_check_report(
    repo: RepoConfig,
    *,
    sha: str,
    event: str,
    source: str,
    publish: bool,
    publish_components: list[str],
    failures: list[str],
    transaction_id: str = "",
    check_mode: str = CHECK_MODE_FULL,
    changed_paths: list[str] | tuple[str, ...] = (),
    changed_files: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
    fast_path_reason: str = "",
) -> dict[str, object]:
    selected_components = publish_components or (
        publish_components_for_report(repo) if publish else []
    )
    components: list[dict[str, object]] = []
    if publish:
        for component in selected_components:
            try:
                tags = compute_registry_tags(repo, sha=sha, component=component)
                release = _component_release_report(repo, component)
                components.append(
                    {
                        "component": component,
                        "dockerhub": tags.dockerhub,
                        "ghcr": tags.ghcr,
                        "upstream_version": tags.upstream_version,
                        "release_package_tag": tags.release_package_tag,
                        "github_release": release,
                    }
                )
            except (Exception, SystemExit) as exc:
                components.append({"component": component, "error": str(exc)})
    return {
        "repo": repo.name,
        "sha": sha,
        "event": event,
        "source": source,
        "publish": publish,
        "transaction_id": transaction_id,
        "check_mode": check_mode,
        "fast_path": {
            "enabled": check_mode == CHECK_MODE_FAST_CLEANUP,
            "reason": fast_path_reason,
            "changed_paths": list(changed_paths),
            "changed_files": list(changed_files),
        },
        "status": "failure" if failures else "success",
        "failures": failures,
        "failure_classes": _failure_classes(failures),
        "publish_attestation": {
            "repo": repo.name,
            "expected_sha": sha,
            "event": event,
            "source": source,
            "control_check_result": "failure" if failures else "success",
            "publish_requested": publish,
            "publish_eligible": publish and not failures,
            "publish_components": selected_components,
            "transaction_id": transaction_id,
        },
        "components": components,
    }


def _changed_paths_from_json(value: str) -> list[str] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "--changed-paths-json must be a JSON array of strings"
        ) from exc
    if not isinstance(payload, list) or not all(
        isinstance(item, str) for item in payload
    ):
        raise ValueError("--changed-paths-json must be a JSON array of strings")
    return payload


def _changed_files_from_json(value: str) -> list[dict[str, str]] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "--changed-files-json must be a JSON array of path/status objects"
        ) from exc
    if not isinstance(payload, list):
        raise ValueError(
            "--changed-files-json must be a JSON array of path/status objects"
        )
    files: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(
                "--changed-files-json must be a JSON array of path/status objects"
            )
        path = _json_arg_text(item.get("path"))
        status = _json_arg_text(item.get("status"))
        previous_path = _json_arg_text(item.get("previous_path"))
        if not path:
            raise ValueError(
                "--changed-files-json entries must include a non-empty path"
            )
        files.append({"path": path, "status": status})
        if previous_path and previous_path != path:
            files.append({"path": previous_path, "status": "renamed-from"})
    return files


def _changed_paths_from_files(files: list[dict[str, str]]) -> list[str]:
    return [item["path"] for item in files]


def _changed_file_statuses_from_files(
    files: list[dict[str, str]] | None,
) -> dict[str, str]:
    if not files:
        return {}
    return {item["path"]: item.get("status", "") for item in files}


def _json_arg_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _failure_classes(failures: list[object]) -> list[str]:
    classes: set[str] = set()
    for failure in failures:
        text = str(failure).lower()
        if any(token in text for token in ["credential", "token", "dockerhub", "ghcr"]):
            classes.add("credential-gap")
        if "delete-scope" in text:
            classes.add("delete-scope-gap")
        if any(token in text for token in ["registry", "missing tag", "unreachable"]):
            classes.add("registry-missing")
        if any(
            token in text
            for token in [
                "prerelease",
                "release changelog version",
                "release_package_tag",
                "retarget",
            ]
        ):
            classes.add("prerelease-mismatch")
        if any(token in text for token in ["checkout", "expected sha", "dirty"]):
            classes.add("checkout-mismatch")
        if any(
            token in text
            for token in [
                "check-run",
                "checks: write",
                "app-check-permission",
                "bootstrap",
            ]
        ):
            classes.add("app-check-permission")
        if "catalog" in text:
            classes.add("catalog-sync-needed")
    return sorted(classes)


def publish_components_for_report(repo: RepoConfig) -> list[str]:
    return publish_components(repo)


def _component_release_report(repo: RepoConfig, component: str) -> dict[str, str]:
    config = component_config(repo, component)
    if str(config.get("release_history", "")).strip() != "github_prerelease":
        return {}
    version = _component_release_version(repo, component=component)
    tag = f"{str(config.get('release_tag_prefix', '') or '')}{version}"
    return {
        "tag": tag,
        "version": version,
        "url": _github_release_url(repo, tag),
        "prerelease": "true",
        "latest": str(bool(config.get("github_release_latest", True))).lower(),
    }


def _publish_github_prerelease(
    repo: RepoConfig, *, component: str, dry_run: bool = False
) -> dict[str, object]:
    config = component_config(repo, component)
    version = _component_release_version(repo, component=component)
    tag = f"{str(config.get('release_tag_prefix', '') or '')}{version}"
    release_package_tag = component_registry_release_tag(repo, component)
    normalized_version = version[1:] if version.startswith("v") else version
    normalized_release_package_tag = (
        release_package_tag[1:]
        if release_package_tag.startswith("v")
        else release_package_tag
    )
    if str(config.get("registry_revision_arg", "") or "").strip() and (
        normalized_release_package_tag != normalized_version
    ):
        print(
            f"{repo.name}:{component}: release changelog version {version} does "
            f"not match registry package tag {release_package_tag or '<missing>'}. "
            "Update the component revision before publishing.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    title = version
    changelog = repo.path / str(config.get("release_changelog", "CHANGELOG.md"))
    notes = extract_release_notes(version, changelog, semver=False)
    release_target = _github_prerelease_target_commit(
        repo, component=component, version=version
    )
    head_target = _git_head(repo.path)
    env = _github_cli_env()
    view = _github_prerelease_view(repo, tag, env=env)
    action = "updated" if view.returncode == 0 else "created"
    target = head_target if action == "created" else release_target
    allowed_existing_targets = {target}
    if action == "updated":
        allowed_existing_targets.add(head_target)
    if action == "updated":
        existing = _github_release_view_data(view.stdout)
        existing_target = str(existing.get("targetCommitish", "") or "").strip()
        if existing_target and existing_target not in allowed_existing_targets:
            print(
                f"{repo.name}:{component}: existing prerelease {tag} targets "
                f"{existing_target}; refusing to retarget immutable release to "
                f"{target}. Bump the component AIO revision or publish from the "
                "original release commit.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if existing_target == head_target:
            target = existing_target
        if _github_prerelease_matches(
            existing,
            target=target,
            title=title,
            notes=notes,
        ):
            return {
                "repo": repo.name,
                "component": component,
                "status": "success",
                "action": "already-present",
                "tag": tag,
                "version": version,
                "target": target,
                "url": _github_release_url(repo, tag),
                "release_package_tag": release_package_tag,
            }
    command = [
        "gh",
        "release",
        "edit" if action == "updated" else "create",
        tag,
        "--repo",
        repo.github_repo,
        "--title",
        title,
        "--notes",
        notes,
        "--prerelease",
        "--latest=false",
    ]
    if action == "created":
        command.extend(["--target", target])
    if dry_run:
        print(" ".join(shlex.quote(part) for part in command))
        dry_action = "would-update" if action == "updated" else "would-create"
        return {
            "repo": repo.name,
            "component": component,
            "status": "success",
            "action": dry_action,
            "tag": tag,
            "version": version,
            "target": target,
            "url": _github_release_url(repo, tag),
            "release_package_tag": release_package_tag,
        }
    result = _run(command, cwd=repo.path, env=env)
    if (
        result.returncode != 0
        and action == "created"
        and _github_release_already_exists(result)
    ):
        view = _github_prerelease_view(repo, tag, env=env)
        if view.returncode == 0:
            existing = _github_release_view_data(view.stdout)
            existing_target = str(existing.get("targetCommitish", "") or "").strip()
            if existing_target and existing_target not in allowed_existing_targets:
                print(
                    f"{repo.name}:{component}: existing prerelease {tag} targets "
                    f"{existing_target}; refusing to retarget immutable release to "
                    f"{target}. Bump the component AIO revision or publish from the "
                    "original release commit.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if _github_prerelease_matches(
                existing,
                target=target,
                title=title,
                notes=notes,
            ):
                return {
                    "repo": repo.name,
                    "component": component,
                    "status": "success",
                    "action": "already-present",
                    "tag": tag,
                    "version": version,
                    "target": target,
                    "url": _github_release_url(repo, tag),
                    "release_package_tag": release_package_tag,
                }
            update_command = [
                "gh",
                "release",
                "edit",
                tag,
                "--repo",
                repo.github_repo,
                "--title",
                title,
                "--notes",
                notes,
                "--prerelease",
                "--latest=false",
            ]
            result = _run(update_command, cwd=repo.path, env=env)
            action = "updated"
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return {
        "repo": repo.name,
        "component": component,
        "status": "success",
        "action": action,
        "tag": tag,
        "version": version,
        "target": target,
        "url": _github_release_url(repo, tag),
        "release_package_tag": release_package_tag,
    }


def _github_prerelease_view(
    repo: RepoConfig, tag: str, *, env: dict[str, str] | None
) -> subprocess.CompletedProcess[str]:
    result = _run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repo.github_repo,
            "--json",
            "targetCommitish,name,body,isPrerelease,isLatest",
        ],
        cwd=repo.path,
        env=env,
    )
    if (
        result.returncode == 0
        or "unknown json field" not in (result.stderr or "").lower()
    ):
        return result
    return _run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repo.github_repo,
            "--json",
            "targetCommitish,name,body,isPrerelease",
        ],
        cwd=repo.path,
        env=env,
    )


def _github_release_view_data(output: str) -> dict[str, object]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {"targetCommitish": output.strip()}
    return data if isinstance(data, dict) else {}


def _github_release_already_exists(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return (
        "already_exists" in text
        or "already exists" in text
        or "release.tag_name" in text
    )


def _github_prerelease_matches(
    release: dict[str, object], *, target: str, title: str, notes: str
) -> bool:
    if str(release.get("targetCommitish", "") or "").strip() != target:
        return False
    if str(release.get("name", "") or "").strip() != title:
        return False
    if str(release.get("body", "") or "").strip() != notes.strip():
        return False
    if release.get("isPrerelease") is not True:
        return False
    if release.get("isLatest") is True:
        return False
    return True


def _github_prerelease_target_commit(
    repo: RepoConfig, *, component: str, version: str
) -> str:
    try:
        return find_release_publish_target_commit(repo.path, version)
    except SystemExit:
        config = component_config(repo, component)
        if str(config.get("release_policy", "")).strip() != "registry_only":
            raise
        if str(config.get("release_history", "")).strip() != "github_prerelease":
            raise
        release_package_tag = component_registry_release_tag(repo, component)
        normalized_version = version[1:] if version.startswith("v") else version
        normalized_release_package_tag = (
            release_package_tag[1:]
            if release_package_tag.startswith("v")
            else release_package_tag
        )
        if normalized_version != normalized_release_package_tag:
            raise
        tracked_paths = [
            str(config.get("dockerfile", "Dockerfile")),
            str(config.get("upstream_config", "upstream.toml")),
            str(config.get("release_changelog", "CHANGELOG.md")),
        ]
        return git(repo.path, "log", "-n", "1", "--format=%H", "--", *tracked_paths)


def _github_cli_env() -> dict[str, str] | None:
    token = (
        os.environ.get("AIO_FLEET_RELEASE_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def _github_release_url(repo: RepoConfig, tag: str) -> str:
    return (
        f"https://github.com/{repo.github_repo}/releases/tag/"
        f"{urllib.parse.quote(tag, safe='')}"
    )


def cmd_validate(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repos = manifest.repos.values() if args.all else [manifest.repo(args.repo)]
    shape_failures: dict[str, list[str]] = {}
    for check in manifest_shape_checks(manifest):
        if check.get("status") == "failed":
            shape_failures.setdefault(str(check.get("repo")), []).append(
                str(check.get("detail"))
            )
    failed = False
    for repo in repos:
        print(f"== {repo.name} ==")
        failures = [
            *shape_failures.get(repo.name, []),
            *_app_manifest_failures(repo),
            *repo_policy_failures(repo, manifest),
        ]
        if failures:
            print("\n".join(failures), file=sys.stderr)
            failed = True
            continue

        generator = str(repo.get("generator_check_command", "") or "").strip()
        if generator:
            result = _run(shlex.split(generator), cwd=repo.path)
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            if result.returncode != 0:
                failed = True
                continue
        print(f"{repo.name} central validation passed")
    return 1 if failed else 0


def cmd_cleanup_repo(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    repos = (
        list(manifest.repos.values())
        if args.all
        else [_repo_for_identifier(manifest, args.repo)]
    )
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]
    failed = False
    report: dict[str, object] = {"repos": []}
    for repo in repos:
        findings = cleanup_findings(repo)
        if findings and (args.remove or args.fix) and not args.dry_run:
            remove_cleanup_findings(findings)
            findings = cleanup_findings(repo)
        if args.verify and findings:
            failed = True
        report["repos"].append(  # type: ignore[index]
            {
                "repo": repo.name,
                "findings": [
                    {
                        "path": str(finding.path.relative_to(repo.path)),
                        "reason": finding.reason,
                    }
                    for finding in findings
                ],
            }
        )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["repos"]:  # type: ignore[index]
            findings = item["findings"]  # type: ignore[index]
            state = "ok" if not findings else f"{len(findings)} retired shared paths"
            print(f"{item['repo']}: cleanup={state}")  # type: ignore[index]
            for finding in findings:
                print(f"- {finding['path']}: {finding['reason']}")
    return 1 if failed else 0


def cmd_security_audit_workflows(args: argparse.Namespace) -> int:
    report = audit_workflows(Path(args.path).resolve())
    if args.format == "json":
        print(stable_report_json(report))
    else:
        if report["findings"]:
            for finding in report["findings"]:
                print(
                    "{path}: {code}: {message}".format(
                        path=finding["path"],
                        code=finding["code"],
                        message=finding["message"],
                    )
                )
        else:
            print("workflow security audit passed")
    return 0 if report["ok"] else 1


def cmd_promote_rehab(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    dashboard = manifest.raw.get("dashboard", {})
    rehab = dashboard.get("rehab_repos", {}) if isinstance(dashboard, dict) else {}
    config = rehab.get(args.repo) if isinstance(rehab, dict) else None
    if not isinstance(config, dict):
        print(f"{args.repo}: not configured as a rehab repo", file=sys.stderr)
        return 1
    path = Path(str(config.get("path", f"../{args.repo}"))).resolve()
    github_repo = str(config.get("github_repo", f"{manifest.owner}/{args.repo}"))
    repo = RepoConfig(
        name=args.repo,
        raw={
            "path": str(path),
            "app_slug": args.repo,
            "image_name": f"wgross19/{args.repo}",
            "docker_cache_scope": f"{args.repo}-image",
            "pytest_image_tag": f"{args.repo}:pytest",
            "github_repo": github_repo,
            "publish_profile": args.profile,
        },
        defaults=manifest.defaults,
        owner=manifest.owner,
    )
    findings: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        findings.append("local checkout is missing")
    else:
        status = _run(["git", "status", "--short"], cwd=path)
        if status.stdout.strip():
            findings.append("local checkout is dirty")
        branch = _run(["git", "branch", "--show-current"], cwd=path)
        if branch.stdout.strip() != "main":
            warnings.append(
                f"local checkout is on {branch.stdout.strip() or 'unknown'}"
            )
        cleanup = cleanup_findings(repo)
        if cleanup:
            findings.append(f"{len(cleanup)} retired shared path(s) remain")
    manifest_entry = {
        "path": str(path),
        "github_repo": github_repo,
        "app_slug": args.repo,
        "image_name": f"wgross19/{args.repo}",
        "docker_cache_scope": f"{args.repo}-image",
        "pytest_image_tag": f"{args.repo}:pytest",
        "publish_profile": args.profile,
    }
    checklist = [
        "rehab repo exists locally and remotely",
        "worktree is clean on main",
        "legacy shared files are removed",
        ".aio-fleet.yml is exported",
        "central validate-repo passes",
        "cleanup-repo --verify passes",
        "control-check dry-run passes",
        "first aio-fleet / required check appears on a real PR",
        "registry and upstream policy are declared",
        "repo is moved from dashboard.rehab_repos into repos",
    ]
    report = {
        "repo": args.repo,
        "mode": "promote-rehab",
        "ready": not findings,
        "manifest_entry": manifest_entry,
        "findings": findings,
        "warnings": warnings,
        "acceptance_checklist": checklist,
        "next_commands": [
            fleet_command(
                "cleanup-repo",
                "--repo",
                args.repo,
                "--repo-path",
                path,
                "--verify",
            ),
            fleet_command("export-app-manifest", "--repo", args.repo, "--write"),
            fleet_command(
                "control-check",
                "--repo",
                args.repo,
                "--repo-path",
                path,
                "--sha",
                "<sha>",
                "--event",
                "pull_request",
                "--dry-run",
            ),
        ],
    }
    if args.format == "json":
        print(stable_report_json(report))
    else:
        state = "ready" if report["ready"] else "blocked"
        print(f"{args.repo}: promote-rehab={state}")
        for finding in findings:
            print(f"- {finding}")
        for warning in warnings:
            print(f"- warning: {warning}")
    return 1 if findings else 0


def cmd_workflow_poll_outputs(args: argparse.Namespace) -> int:
    report = poll_outputs(
        report_path=Path(args.input),
        run_checks=args.run_checks,
        github_output=Path(args.github_output) if args.github_output else None,
    )
    print(stable_report_json(report))
    return 0


def cmd_workflow_upstream_poll_targets(args: argparse.Namespace) -> int:
    report = upstream_poll_targets(
        report_path=Path(args.input),
        manifest_path=Path(args.manifest),
        github_output=Path(args.github_output) if args.github_output else None,
    )
    print(stable_report_json(report))
    return 0


def cmd_workflow_upstream_summary(args: argparse.Namespace) -> int:
    text = render_upstream_summary(
        report_path=Path(args.input),
        output_path=Path(args.output) if args.output else None,
    )
    print(text, end="")
    return 0


def cmd_workflow_registry_summary(args: argparse.Namespace) -> int:
    text = render_registry_summary(
        report_path=Path(args.input),
        status=args.status,
        output_path=Path(args.output) if args.output else None,
    )
    print(text, end="")
    return 0


def cmd_workflow_checkout_dashboard(args: argparse.Namespace) -> int:
    report = checkout_dashboard_repos(
        manifest_path=Path(args.manifest),
        checkout_root=Path(args.checkout_root),
        output_manifest=Path(args.output_manifest),
        token=args.token or _workflow_token(),
    )
    print(stable_report_json(report))
    return 0


def cmd_workflow_checkout_upstream(args: argparse.Namespace) -> int:
    report = checkout_upstream_monitor_repos(
        manifest_path=Path(args.manifest),
        checkout_root=Path(args.checkout_root),
        output_manifest=Path(args.output_manifest),
        output_path=Path(args.output) if args.output else None,
        token=args.token or _workflow_token(),
    )
    print(stable_report_json(report))
    return 0


def cmd_workflow_upstream_monitor(args: argparse.Namespace) -> int:
    report = upstream_monitor_checkouts(
        manifest_path=Path(args.manifest),
        output_path=Path(args.output),
        mutate=args.mutate,
        dry_run=args.dry_run,
    )
    print(stable_report_json(report))
    return int(report.get("status", 0))


def cmd_workflow_upstream_actions(args: argparse.Namespace) -> int:
    report = apply_upstream_monitor_actions(
        manifest_path=Path(args.manifest),
        checkout_root=Path(args.checkout_root),
        report_path=Path(args.input),
        output_path=Path(args.output),
    )
    print(stable_report_json(report))
    return int(report.get("status", 0))


def cmd_workflow_upstream_validate(args: argparse.Namespace) -> int:
    report = validate_upstream_monitor_report(
        manifest_path=Path(args.manifest),
        checkout_root=Path(args.checkout_root),
        report_path=Path(args.input),
        output_path=Path(args.output),
    )
    print(stable_report_json(report))
    return int(report.get("status", 0))


def cmd_workflow_registry_audit(args: argparse.Namespace) -> int:
    report = registry_audit_checkouts(
        manifest_path=Path(args.manifest),
        checkout_root=Path(args.checkout_root),
        output_path=Path(args.output),
        token=args.token or _workflow_token(),
        github_output=Path(args.github_output) if args.github_output else None,
    )
    print(stable_report_json(report))
    return int(report.get("status", 0))


def cmd_workflow_smoke_test(args: argparse.Namespace) -> int:
    report = smoke_published_images(
        manifest_path=Path(args.manifest),
        arch=args.arch,
        output_path=Path(args.output) if args.output else None,
        github_output=Path(args.github_output) if args.github_output else None,
    )
    print(stable_report_json(report))
    return int(report.get("status", 0))


def _workflow_token() -> str:
    return os.environ.get("AIO_FLEET_WORKFLOW_TOKEN", "") or os.environ.get(
        "APP_TOKEN", ""
    )


def cmd_trunk_audit(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repos = [manifest.repo(args.repo)] if args.repo else manifest.repos.values()
    failed = False
    for repo in repos:
        trunk_config = repo.path / ".trunk" / "trunk.yaml"
        if not trunk_config.exists():
            print(f"{repo.name}: trunk=skipped:no-config")
            continue
        command = [
            "trunk",
            "check",
            "--show-existing",
            "--all",
            "--no-fix",
            "--no-progress",
            "--color=false",
        ]
        result = _run(command, cwd=repo.path)
        if result.returncode == 0:
            print(f"{repo.name}: trunk=ok")
            continue
        failed = True
        issue_line = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if "issues" in line.lower()
            ),
            "",
        )
        detail = f" {issue_line}" if issue_line else ""
        print(f"{repo.name}: trunk=failed exit={result.returncode}{detail}")
        if args.verbose:
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
    return 1 if failed else 0


def cmd_trunk_run(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        if args.local and args.repo_path:
            repo_path = Path(args.repo_path).resolve()
            repos = [_local_repo_config(repo_path, owner=manifest.owner)]
        else:
            print("--repo is required unless --all is used", file=sys.stderr)
            return 1
    else:
        repos = (
            list(manifest.repos.values())
            if args.all
            else [_repo_for_identifier(manifest, args.repo)]
        )
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        if args.repo:
            repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]
    failed = False
    for repo in repos:
        if args.local:
            result = run_local_trunk_overlay(
                repo, fix=args.fix, all_files=args.all_files
            )
        else:
            result = run_central_trunk(repo, fix=args.fix)
        if result.returncode == 0:
            print(f"{repo.name}: trunk=ok")
            continue
        failed = True
        print(f"{repo.name}: trunk=failed exit={result.returncode}")
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
    return 1 if failed else 0


def _local_repo_config(repo_path: Path, *, owner: str) -> RepoConfig:
    name = repo_path.name
    return RepoConfig(
        name=name,
        raw={
            "path": repo_path,
            "app_slug": name,
            "image_name": f"wgross19/{name}",
            "docker_cache_scope": f"{name}-image",
            "pytest_image_tag": f"{name}:pytest",
        },
        defaults={},
        owner=owner,
    )


def cmd_hooks_install(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    if not args.all and not args.repo:
        print("--repo is required unless --all is used", file=sys.stderr)
        return 1
    repos = (
        list(manifest.repos.values())
        if args.all
        else [_repo_for_identifier(manifest, args.repo)]
    )
    if args.all and args.include_destinations:
        repos.extend(_dashboard_destination_repos(manifest))
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]
    for repo in repos:
        hooks_dir = install_local_hooks(
            repo, target_kind=str(repo.raw.get("_hook_target_kind", "repo"))
        )
        print(f"{repo.name}: hooks=installed path={hooks_dir}")
    return 0


def _dashboard_destination_repos(manifest: FleetManifest) -> list[RepoConfig]:
    destination_repos = (
        manifest.raw.get("dashboard", {}).get("destination_repos", {})
        if isinstance(manifest.raw.get("dashboard"), dict)
        else {}
    )
    repos: list[RepoConfig] = []
    for name, raw_config in destination_repos.items():
        if not isinstance(raw_config, dict) or "path" not in raw_config:
            continue
        repos.append(
            RepoConfig(
                name=str(name),
                raw={
                    "path": raw_config["path"],
                    "app_slug": name,
                    "image_name": f"wgross19/{name}",
                    "docker_cache_scope": f"{name}-image",
                    "pytest_image_tag": f"{name}:pytest",
                    "_hook_target_kind": "catalog",
                },
                defaults={},
                owner=manifest.owner,
            )
        )
    return repos


def cmd_import_app_manifest(args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()
    data = load_app_manifest(path)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_sync_catalog(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repos = (
        [_repo_for_identifier(manifest, args.repo)]
        if args.repo
        else list(manifest.repos.values())
    )
    if args.repo_path:
        if len(repos) != 1:
            print("--repo-path can only be used with --repo", file=sys.stderr)
            return 1
        repos = [_repo_with_path(repos[0], Path(args.repo_path).resolve())]
    blocked = unpublished_xml_targets(manifest, repos)
    if blocked and not args.icon_only:
        print(
            "refusing XML sync for catalog_published=false repos; use --icon-only for staged launches:\n"
            + "\n".join(blocked),
            file=sys.stderr,
        )
        return 1

    changes = sync_catalog_assets(
        manifest,
        catalog_path=Path(args.catalog_path).resolve(),
        repos=repos,
        icon_only=args.icon_only,
        dry_run=args.dry_run,
    )
    for change in changes:
        prefix = "would " if args.dry_run else ""
        print(
            f"{change.repo}: {prefix}{change.action} "
            f"{change.target.relative_to(Path(args.catalog_path).resolve())}"
        )
    print(f"catalog changes: {len(changes)}")
    if changes and args.create_pr:
        _catalog_commit_and_pr(
            Path(args.catalog_path).resolve(),
            branch=args.branch or _catalog_branch(args.repo, args.icon_only),
            base=args.base,
            title=args.title or _catalog_title(args.repo, args.icon_only),
            body=args.body or _catalog_body(args.repo, args.icon_only),
            paths=[change.target for change in changes],
            dry_run=args.dry_run,
        )
    return 0


def cmd_infra_doctor(args: argparse.Namespace) -> int:
    root = Path.cwd()
    infra_path = Path(args.path).resolve()
    manifest = load_manifest(Path(args.manifest))
    policy_path = Path(args.policy)
    failures: list[str] = []

    if not args.skip_tofu and shutil.which("tofu") is None:
        failures.append("OpenTofu CLI is not installed")
    if not (infra_path / ".terraform.lock.hcl").exists():
        failures.append(f"{infra_path}: missing .terraform.lock.hcl")
    if not args.skip_tofu and not (infra_path / ".terraform").exists():
        failures.append(f"{infra_path}: OpenTofu is not initialized; run tofu init")

    failures.extend(tracked_artifact_failures(root))
    for relative in [
        "infra/github/terraform.tfstate",
        "infra/github/terraform.tfstate.backup",
        "infra/github/repos.tfvars",
    ]:
        if not _git_check_ignore(root, relative):
            failures.append(f"{relative} is not ignored by git")

    try:
        policy = load_policy(policy_path)
        policy_repos = set(policy["repositories"])
        expected = {repo.name for repo in manifest.repos.values()}
        expected.add("aio-fleet")
        expected.add("awesome-unraid")
        missing = sorted(expected - policy_repos)
        extra = sorted(policy_repos - expected)
        if missing:
            failures.append(f"github policy missing repos: {missing}")
        if extra:
            failures.append(f"github policy has unknown repos: {extra}")
    except Exception as exc:
        failures.append(f"unable to load GitHub policy: {exc}")

    if not args.skip_tofu and shutil.which("tofu") is not None:
        for command in (["tofu", "fmt", "-check", "-recursive"], ["tofu", "validate"]):
            result = _run(command, cwd=infra_path)
            if result.returncode != 0:
                failures.append(
                    f"{' '.join(command)} failed: {(result.stderr or result.stdout).strip()}"
                )

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("infra doctor passed")
    return 0


def cmd_signing_doctor(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    report = signing_doctor_report(
        manifest,
        repos=args.repo,
        all_targets=args.all,
        env=os.environ,
        include_hooks=not args.no_hooks,
    )
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in report["checks"]:
            repo = f"{check.get('repo')}: " if check.get("repo") else ""
            print(
                "{status} {classification}: {repo}{detail}".format(
                    status=check["status"],
                    classification=check.get("classification", check.get("class")),
                    repo=repo,
                    detail=check["detail"],
                )
            )
        print(
            "signing doctor {status}: {failed} failed, {warnings} warning(s), {checks} checks".format(
                status=report["status"],
                **report["summary"],
            )
        )
    return 1 if report["summary"]["failed"] else 0


def _git_check_ignore(repo_path: Path, relative_path: str) -> bool:
    result = _run(["git", "check-ignore", "--quiet", relative_path], cwd=repo_path)
    return result.returncode == 0


def cmd_onboard_repo(args: argparse.Namespace) -> int:
    mode = getattr(args, "mode", "existing")
    shape = getattr(args, "shape", None) or (
        "rehab-only" if mode == "rehab" else "single-image"
    )
    image_name = args.image_name or f"wgross19/{args.repo}"
    upstream_name = args.upstream_name or args.repo.removesuffix("-aio").title()
    local_path_base = args.local_path_base.rstrip("/")
    manifest_entry = {
        "path": f"{local_path_base}/{args.repo}",
        "public": True,
        "workflow_name": f"CI / {upstream_name} AIO",
        "app_slug": args.repo,
        "image_name": image_name,
        "docker_cache_scope": f"{args.repo}-image",
        "pytest_image_tag": f"{args.repo}:pytest",
        "publish_profile": args.profile,
        "release_name": f"{upstream_name} AIO",
        "upstream_name": upstream_name,
        "image_description": f"Unraid-first AIO wrapper image for {upstream_name}",
        "xml_paths": [f"{args.repo}.xml"],
        "catalog_assets": [
            {"source": f"{args.repo}.xml", "target": f"{args.repo}.xml"}
        ],
    }
    component_publish: list[dict[str, object]] = []
    component_notes: list[str] = []
    dashboard_entry: dict[str, object] | None = None
    if shape == "multi-component":
        pack = _multi_component_onboarding_pack(
            args.repo,
            upstream_name=upstream_name,
            image_name=image_name,
        )
        manifest_update = pack.get("manifest", {})
        if isinstance(manifest_update, dict):
            manifest_entry.update(manifest_update)
        raw_component_publish = pack.get("component_publish", [])
        if isinstance(raw_component_publish, list):
            component_publish = [
                item for item in raw_component_publish if isinstance(item, dict)
            ]
        raw_component_notes = pack.get("notes", [])
        if isinstance(raw_component_notes, list):
            component_notes = [str(item) for item in raw_component_notes]
    elif shape == "submodule-backed":
        manifest_entry["submodules"] = [
            {
                "path": "upstream",
                "tracking": "manifest-declared upstream provider",
            }
        ]
    elif shape == "destination-only":
        manifest_entry = {}
        dashboard_entry = {
            args.repo: {
                "path": f"{local_path_base}/{args.repo}",
                "github_repo": f"wgross19/{args.repo}",
                "kind": "destination",
                "catalog_path": f"{local_path_base}/{args.repo}",
            }
        }
    elif shape == "rehab-only":
        dashboard_entry = {
            args.repo: {
                "path": f"{local_path_base}/{args.repo}",
                "github_repo": f"wgross19/{args.repo}",
                "status": "rehab",
                "next_action": "run rehab onboarding",
            }
        }
    acceptance_checklist = [
        "repo exists or is created from unraid-aio-template",
        "fleet.yml entry added with Docker Hub image name",
        ".aio-fleet.yml exported into the app repo",
        "central repo validation passes",
        "cleanup-repo --verify reports no retired shared files",
        "control-check dry-run succeeds for the app commit",
        "upstream monitor dry-run resolves provider state",
        "registry verify dry-run prints expected Docker Hub and GHCR tags",
        "catalog sync dry-run shows expected XML/icon assets",
        "support-thread render produces a CA-ready draft",
        "aio-fleet / required appears on a real app PR",
    ]
    acceptance_checklist.extend(_onboarding_shape_checklist(shape))
    creation_steps = _onboarding_creation_steps(args.repo, mode, shape)
    first_commands = _onboarding_first_commands(
        args.repo,
        mode,
        shape,
        component_publish=component_publish,
    )
    if mode == "rehab":
        acceptance_checklist = [
            "local repo synced to main",
            "Dockerfile, runtime wrapper, XML, README, and support docs inspected",
            "publish profile and upstream monitor strategy decided",
            "fleet.yml entry added or updated",
            ".aio-fleet.yml exported into the app repo",
            "legacy workflows/config/scripts removed",
            "central validation and cleanup verification pass",
            "aio-fleet / required appears on a real rehab PR",
            "repo promoted to active fleet only after validation passes",
            *_onboarding_shape_checklist(shape),
        ]
    if args.format == "json":
        print(
            json.dumps(
                {
                    "repo": args.repo,
                    "mode": mode,
                    "shape": shape,
                    "manifest_entry": manifest_entry,
                    "dashboard_entry": dashboard_entry,
                    "component_publish": component_publish,
                    "component_notes": component_notes,
                    "creation_steps": creation_steps,
                    "first_commands": first_commands,
                    "acceptance_checklist": acceptance_checklist,
                },
                indent=2,
            )
        )
        return 0

    print(f"# Onboard {args.repo}")
    print()
    print(f"Mode: `{mode}`")
    print(f"Shape: `{shape}`")
    print()
    if creation_steps:
        print("## Creation / Intake Steps")
        print()
        for step in creation_steps:
            print(f"- {step}")
        print()
    print("## Manifest entry")
    print()
    print("```yaml")
    if manifest_entry:
        print(_yaml_block({args.repo: manifest_entry}, indent=2))
    else:
        print(
            "  # no active repos entry; add this under dashboard.destination_repos or dashboard.rehab_repos"
        )
        if dashboard_entry:
            print(_yaml_block(dashboard_entry, indent=2))
    print("```")
    print()
    if component_publish or component_notes:
        print("## Component publish behavior")
        print()
        for item in component_publish:
            component = item.get("component", "")
            behavior = item.get("publish_behavior", item.get("release_policy", ""))
            image = item.get("image_name", "")
            print(f"- `{component}`: {behavior}; image `{image}`")
        for note in component_notes:
            print(f"- {note}")
        print()
    print("## First commands")
    print()
    for command in first_commands:
        print(f"- `{command}`")
    print()
    print("## Acceptance checklist")
    print()
    for item in acceptance_checklist:
        print(f"- [ ] {item}")
    return 0


def _multi_component_onboarding_pack(
    repo: str, *, upstream_name: str, image_name: str
) -> OnboardingPack:
    slug = repo.removesuffix("-aio")
    normalized_upstream = upstream_name.lower().replace(" ", "")
    if repo == "penpot-aio" or normalized_upstream == "penpot":
        return _penpot_component_pack(repo, image_name=image_name)
    if repo == "nanoclaw-aio" or normalized_upstream == "nanoclaw":
        return _nanoclaw_component_pack(repo, image_name=image_name)
    return _generic_published_component_pack(
        repo,
        slug=slug,
        upstream_name=upstream_name,
        image_name=image_name,
    )


def _penpot_component_pack(repo: str, *, image_name: str) -> OnboardingPack:
    return {
        "manifest": {
            "publish_profile": "changelog-version",
            "upstream_version_key": "PENPOT_VERSION",
            "upstream_digest_arg": "PENPOT_BACKEND_DIGEST",
            "generated_template": True,
            "generator_check_command": (
                "python3 scripts/generate_penpot_template.py --check"
            ),
            "upstream_commit_paths": [
                "Dockerfile",
                f"{repo}.xml",
                "docs/upstream/penpot-config-inventory.json",
            ],
            "upstream_monitor": [
                _digest_monitor(
                    "frontend",
                    "Penpot Frontend",
                    "penpot/penpot",
                    "penpotapp/frontend",
                    "PENPOT_VERSION",
                    "PENPOT_FRONTEND_DIGEST",
                ),
                _digest_monitor(
                    "backend",
                    "Penpot Backend",
                    "penpot/penpot",
                    "penpotapp/backend",
                    "PENPOT_VERSION",
                    "PENPOT_BACKEND_DIGEST",
                ),
                _digest_monitor(
                    "exporter",
                    "Penpot Exporter",
                    "penpot/penpot",
                    "penpotapp/exporter",
                    "PENPOT_VERSION",
                    "PENPOT_EXPORTER_DIGEST",
                ),
                _digest_monitor(
                    "mcp",
                    "Penpot MCP",
                    "penpot/penpot",
                    "penpotapp/mcp",
                    "PENPOT_VERSION",
                    "PENPOT_MCP_DIGEST",
                ),
                _digest_monitor(
                    "mailpit",
                    "Mailpit",
                    "axllent/mailpit",
                    "axllent/mailpit",
                    "MAILPIT_VERSION",
                    "MAILPIT_IMAGE_DIGEST",
                ),
            ],
            "extended_integration": {
                "input_name": "run_extended_integration",
                "description": (
                    "Run optional external PostgreSQL/Redis/S3/SMTP "
                    "integration tests"
                ),
                "pytest_args": "tests/integration -m extended_integration",
            },
            "xml_paths": ["*.xml", "assets/**"],
            "catalog_assets": [
                {"source": f"{repo}.xml", "target": f"{repo}.xml"},
                {"source": "assets/app-icon.png", "target": "icons/penpot.png"},
            ],
            "validation": {
                "required_targets": [
                    "/appdata",
                    "8080",
                    "8025",
                    "PENPOT_AIO_DEFAULT_FLAGS",
                    "PENPOT_AIO_ENABLE_INTERNAL_POSTGRES",
                    "PENPOT_AIO_ENABLE_INTERNAL_REDIS",
                    "PENPOT_AIO_ENABLE_MAILPIT",
                    "PENPOT_AIO_ENABLE_MCP",
                    "PENPOT_DATABASE_URI",
                    "PENPOT_PUBLIC_URI",
                    "PENPOT_SECRET_KEY",
                ],
                "required_text_fields": ["ReadMe"],
                "exact_category_tokens": ["Productivity", "Tools:Utilities"],
            },
        },
        "component_publish": [
            {
                "component": "aio",
                "image_name": image_name,
                "release_policy": "formal_release_and_registry",
                "publish_behavior": "published AIO wrapper image",
            },
            *[
                {
                    "component": component,
                    "image_name": upstream_image,
                    "release_policy": "upstream_digest_only",
                    "publish_behavior": (
                        "monitored upstream input; not published by aio-fleet"
                    ),
                }
                for component, upstream_image in (
                    ("frontend", "penpotapp/frontend"),
                    ("backend", "penpotapp/backend"),
                    ("exporter", "penpotapp/exporter"),
                    ("mcp", "penpotapp/mcp"),
                    ("mailpit", "axllent/mailpit"),
                )
            ],
        ],
        "notes": [
            (
                "Penpot has multiple monitored upstream images but still "
                "publishes one AIO wrapper image."
            ),
            (
                "Do not add `components` unless the repo is intentionally split "
                "into separately published images."
            ),
        ],
    }


def _nanoclaw_component_pack(repo: str, *, image_name: str) -> OnboardingPack:
    slug = repo.removesuffix("-aio")
    agent_image = f"wgross19/{slug}-agent"
    agent_dockerfile = f"components/{slug}-agent/Dockerfile"
    return {
        "manifest": {
            "publish_profile": "multi-component",
            "runtime_supervisor": "s6",
            "runtime_healthcheck_markers": ['pgrep -f "dist/index.js"'],
            "check_upstream_name": "Check NanoClaw Upstream",
            "upstream_components": [repo, f"{slug}-agent"],
            "upstream_commit_paths": ["Dockerfile", agent_dockerfile],
            "upstream_monitor": [
                {
                    "component": "aio",
                    "name": "NanoClaw",
                    "source": "github-releases",
                    "repo": "nanocoai/nanoclaw",
                    "dockerfile": "Dockerfile",
                    "version_key": "UPSTREAM_VERSION",
                    "stable_only": True,
                    "strategy": "pr",
                    "release_notes_url": (
                        "https://github.com/nanocoai/nanoclaw/releases"
                    ),
                },
                {
                    "component": "agent",
                    "name": "NanoClaw Agent",
                    "source": "github-releases",
                    "repo": "nanocoai/nanoclaw",
                    "dockerfile": agent_dockerfile,
                    "version_key": "UPSTREAM_VERSION",
                    "stable_only": True,
                    "strategy": "pr",
                    "release_notes_url": (
                        "https://github.com/nanocoai/nanoclaw/releases"
                    ),
                },
            ],
            "publish_platforms": "linux/amd64",
            "catalog_assets": [{"source": f"{repo}.xml", "target": f"{repo}.xml"}],
            "components": {
                "aio": {
                    "image_name": image_name,
                    "dockerfile": "Dockerfile",
                    "upstream_config": "upstream.toml",
                    "docker_cache_scope": f"{repo}-image",
                    "oci_description": (
                        "Telegram-first Unraid AIO wrapper for NanoClaw v2"
                    ),
                    "release_suffix": "aio",
                    "floating_tags": ["latest"],
                    "upstream_version_key": "UPSTREAM_VERSION",
                    "xml_paths": [f"{repo}.xml"],
                },
                "agent": {
                    "image_name": agent_image,
                    "dockerfile": agent_dockerfile,
                    "context": f"components/{slug}-agent",
                    "upstream_config": "upstream.toml",
                    "docker_cache_scope": f"{slug}-agent-image",
                    "oci_description": f"Helper sandbox image spawned by {repo}",
                    "release_policy": "registry_only",
                    "release_suffix": "agent",
                    "registry_revision_arg": "AGENT_REVISION",
                    "floating_tags": ["latest"],
                    "upstream_version_key": "UPSTREAM_VERSION",
                },
            },
            "validation": {
                "docker_socket_required": True,
                "required_targets": [
                    "/appdata",
                    "/var/run/docker.sock",
                    "TELEGRAM_BOT_TOKEN",
                    "ANTHROPIC_API_KEY",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "ONECLI_URL",
                    "ONECLI_API_KEY",
                    "CONTAINER_IMAGE",
                    "CONTAINER_IMAGE_BASE",
                    "CONTAINER_TIMEOUT",
                    "IDLE_TIMEOUT",
                    "CONTAINER_MAX_OUTPUT_SIZE",
                    "MAX_MESSAGES_PER_PROMPT",
                    "MAX_CONCURRENT_CONTAINERS",
                    "LOG_LEVEL",
                ],
            },
        },
        "component_publish": [
            {
                "component": "aio",
                "image_name": image_name,
                "release_policy": "formal_release_and_registry",
                "publish_behavior": (
                    "published Unraid-facing AIO image and release lane"
                ),
            },
            {
                "component": "agent",
                "image_name": agent_image,
                "release_policy": "registry_only",
                "publish_behavior": (
                    "published helper image without its own Community Apps XML"
                ),
            },
        ],
        "notes": [
            (
                "NanoClaw publishes a helper image, but only the AIO XML is "
                "catalog-facing."
            ),
            (
                "`release_policy: registry_only` keeps the helper out of "
                "formal GitHub Release/changelog flow."
            ),
        ],
    }


def _generic_published_component_pack(
    repo: str, *, slug: str, upstream_name: str, image_name: str
) -> OnboardingPack:
    helper = "helper"
    helper_image = f"wgross19/{slug}-{helper}"
    helper_dockerfile = f"components/{slug}-{helper}/Dockerfile"
    return {
        "manifest": {
            "publish_profile": "multi-component",
            "upstream_components": [repo, f"{slug}-{helper}"],
            "upstream_commit_paths": ["Dockerfile", helper_dockerfile],
            "components": {
                "aio": {
                    "image_name": image_name,
                    "dockerfile": "Dockerfile",
                    "docker_cache_scope": f"{repo}-image",
                    "release_suffix": "aio",
                    "floating_tags": ["latest"],
                    "upstream_version_key": "UPSTREAM_VERSION",
                    "xml_paths": [f"{repo}.xml"],
                },
                helper: {
                    "image_name": helper_image,
                    "dockerfile": helper_dockerfile,
                    "context": f"components/{slug}-{helper}",
                    "docker_cache_scope": f"{slug}-{helper}-image",
                    "release_policy": "registry_only",
                    "release_suffix": helper,
                    "registry_revision_arg": f"{helper.upper()}_REVISION",
                    "floating_tags": ["latest"],
                    "upstream_version_key": "UPSTREAM_VERSION",
                },
            },
        },
        "component_publish": [
            {
                "component": "aio",
                "image_name": image_name,
                "release_policy": "formal_release_and_registry",
                "publish_behavior": "published Unraid-facing AIO image",
            },
            {
                "component": helper,
                "image_name": helper_image,
                "release_policy": "registry_only",
                "publish_behavior": (
                    "published helper image; replace this placeholder with "
                    "the real component contract"
                ),
            },
        ],
        "notes": [
            (
                f"Replace the helper component with the actual {upstream_name} "
                "component name before adding this to fleet.yml."
            ),
            (
                "If the extra components are only upstream images bundled into "
                "one AIO image, use the Penpot-style monitor-only pack instead."
            ),
        ],
    }


def _digest_monitor(
    component: str,
    name: str,
    repo: str,
    image: str,
    version_key: str,
    digest_key: str,
) -> dict[str, object]:
    return {
        "component": component,
        "name": name,
        "source": "github-releases",
        "repo": repo,
        "image": image,
        "digest_source": "dockerhub",
        "dockerfile": "Dockerfile",
        "version_key": version_key,
        "digest_key": digest_key,
        "stable_only": True,
        "strategy": "pr",
        "release_notes_url": f"https://github.com/{repo}/releases",
    }


def _onboarding_shape_checklist(shape: str) -> list[str]:
    if shape == "multi-component":
        return [
            (
                "published components have declared images, Dockerfile/context, "
                "registry tags, and publish policy"
            ),
            (
                "upstream-only components are monitored but not treated as "
                "separately published images"
            ),
            "component-specific registry verify passes for every published component",
        ]
    if shape == "submodule-backed":
        return [
            "submodule paths are declared and initialized in central checkouts",
            "upstream monitor updates gitlinks without local unsigned commits",
        ]
    if shape == "destination-only":
        return [
            "repo is dashboard-visible but excluded from app validation and publish automation",
            "catalog/destination health is collected without app release state",
        ]
    if shape == "rehab-only":
        return [
            "repo stays non-blocking until promote-rehab reports ready",
            "retired shared files are removed before active fleet promotion",
        ]
    return []


def _onboarding_creation_steps(repo: str, mode: str, shape: str) -> list[str]:
    if mode == "new-from-template":
        steps = [
            f"create wgross19/{repo} from wgross19/unraid-aio-template",
            f"clone wgross19/{repo} locally",
            "keep only app-specific runtime/source/template/docs/tests in the repo",
            "add the repo to fleet.yml before exporting .aio-fleet.yml",
        ]
        if shape == "submodule-backed":
            steps.append("add and commit required submodule declarations before export")
        return steps
    if mode == "rehab":
        return [
            f"treat existing wgross19/{repo} as a non-blocking rehab repo",
            "sync local checkout to main before editing",
            "audit legacy files that aio-fleet now replaces",
            "do not promote to active fleet until central validation passes",
        ]
    return []


def _onboarding_first_commands(
    repo: str,
    mode: str,
    shape: str,
    *,
    component_publish: list[dict[str, object]] | None = None,
) -> list[str]:
    commands = []
    if mode == "rehab":
        commands.extend(
            [
                f"git -C ../{repo} fetch --prune origin",
                fleet_command(
                    "onboard-repo",
                    "--repo",
                    repo,
                    "--mode",
                    "rehab",
                    "--format",
                    "json",
                ),
                "# after adding the repo to fleet.yml:",
            ]
        )
    if shape == "submodule-backed":
        commands.append(f"git -C ../{repo} submodule update --init --recursive")
    if shape in {"destination-only", "rehab-only"} and mode != "rehab":
        return [
            fleet_command(
                "fleet-dashboard", "update", "--dry-run", "--include-activity"
            ),
            fleet_command(
                "fleet-report", "generate", "--include-activity", "--format", "json"
            ),
        ]
    commands.extend(
        [
            fleet_command("export-app-manifest", "--repo", repo, "--write"),
            fleet_command("validate-repo", "--repo", repo, "--repo-path", f"../{repo}"),
            fleet_command(
                "sync-catalog",
                "--repo",
                repo,
                "--catalog-path",
                "../awesome-unraid",
                "--dry-run",
            ),
            fleet_command("upstream", "monitor", "--repo", repo, "--dry-run"),
        ]
    )
    publish_components = _onboarding_publish_components(component_publish or [])
    if shape == "multi-component" and publish_components:
        for component in publish_components:
            commands.append(
                fleet_command(
                    "registry",
                    "verify",
                    "--repo",
                    repo,
                    "--component",
                    component,
                    "--sha",
                    "<commit-sha>",
                    "--dry-run",
                    "--verbose",
                )
            )
    else:
        commands.append(
            fleet_command(
                "registry",
                "verify",
                "--repo",
                repo,
                "--sha",
                "<commit-sha>",
                "--dry-run",
                "--verbose",
            )
        )
    commands.append(fleet_command("support-thread", "render", "--repo", repo))
    if mode == "new-from-template":
        commands.insert(
            0,
            f"gh repo create wgross19/{repo} --template wgross19/unraid-aio-template --public",
        )
    return commands


def _onboarding_publish_components(
    component_publish: list[dict[str, object]],
) -> list[str]:
    publish_components: list[str] = []
    for item in component_publish:
        policy = str(item.get("release_policy", ""))
        if policy == "upstream_digest_only":
            continue
        component = str(item.get("component", "")).strip()
        if component:
            publish_components.append(component)
    return publish_components


def cmd_export_app_manifest(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    rendered = render_app_manifest(repo)
    output = (
        Path(args.output).resolve() if args.output else repo.path / APP_MANIFEST_NAME
    )
    if args.write:
        output.write_text(rendered)
        print(f"wrote {output}")
        return 0
    print(rendered, end="")
    return 0


def _yaml_line(key: str, value: object, *, indent: int) -> str:
    prefix = " " * indent
    if isinstance(value, list):
        lines = [f"{prefix}{key}:"]
        for item in value:
            if isinstance(item, dict):
                entries = list(item.items())
                if not entries:
                    lines.append(f"{prefix}  - {{}}")
                    continue
                first_key, first_value = entries[0]
                lines.append(f"{prefix}  - {first_key}: {first_value}")
                for item_key, item_value in entries[1:]:
                    lines.append(f"{prefix}    {item_key}: {item_value}")
            else:
                lines.append(f"{prefix}  - {item}")
        return "\n".join(lines)
    if isinstance(value, bool):
        return f"{prefix}{key}: {str(value).lower()}"
    return f"{prefix}{key}: {value}"


def _yaml_block(value: object, *, indent: int) -> str:
    rendered = yaml.safe_dump(
        value,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    prefix = " " * indent
    return "\n".join(
        f"{prefix}{line}" if line else line for line in rendered.splitlines()
    )


def cmd_support_thread_render(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    repo = _repo_for_identifier(manifest, args.repo)
    xml_path = _first_xml_source(repo)
    xml_values = _xml_text_values(repo.path / xml_path) if xml_path else {}
    template_path = (
        Path(__file__).resolve().parents[2] / "docs" / "support-thread-template.md"
    )
    text = template_path.read_text()
    replacements = {
        "{{APP_NAME}}": repo.app_slug,
        "{{SHORT_DESCRIPTOR}}": repo.get("upstream_name", repo.app_slug),
        "{{ONE_SENTENCE_APP_DESCRIPTION}}": _app_description(
            _first_sentence(xml_values.get("Overview", "")),
            str(repo.get("upstream_name", repo.app_slug)),
        ),
        "{{UPSTREAM_APP_NAME}}": str(repo.get("upstream_name", repo.app_slug)),
        "{{IMAGE_NAME}}": repo.image_name,
        "{{WEBUI_URL_OR_NOTE}}": _webui_note(xml_values),
        "{{APPDATA_PATHS}}": "/appdata",
        "{{REQUIRED_FIELDS}}": "Use the default template values unless the app README says otherwise.",
        "{{FIRST_BOOT_EXPECTATIONS}}": "First boot may take several minutes while bundled services initialize.",
        "{{LIMITATION_1}}": "AIO packaging trades service separation for easier Unraid installation.",
        "{{LIMITATION_2}}": "Advanced external dependencies remain app-specific.",
        "{{LIMITATION_3}}": "Back up appdata before upgrades.",
        "{{PATH_1}}": "/appdata",
        "{{PATH_2}}": "See the Unraid template for app-specific persisted paths.",
        "{{PATH_3}}": "See the source README for optional external service paths.",
        "{{PROJECT_REPO_URL}}": f"https://github.com/{repo.github_repo}",
        "{{UPSTREAM_URL}}": xml_values.get("Project", ""),
        "{{CATALOG_REPO_URL}}": "https://github.com/wgross19/awesome-unraid",
        "{{GITHUB_SPONSORS_URL}}": "https://github.com/sponsors/wgross19",
        "{{KOFI_URL}}": "https://ko-fi.com/wgross19",
        "{{MAINTAINER_NAME}}": "wgross19",
        "{{GITHUB_PROFILE_URL}}": "https://github.com/wgross19",
        "{{PORTFOLIO_URL}}": "https://aethereal.dev",
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, str(value))
    print(text)
    return 0


def _first_xml_source(repo: RepoConfig) -> str:
    assets = repo.raw.get("catalog_assets", [])
    if not isinstance(assets, list):
        return ""
    for asset in assets:
        if isinstance(asset, dict):
            source = str(asset.get("source", ""))
            if source.endswith(".xml"):
                return source
    return ""


def _xml_text_values(xml_path: Path) -> dict[str, str]:
    if not xml_path.exists():
        return {}
    import defusedxml.ElementTree as ET

    root = ET.parse(xml_path).getroot()
    return {child.tag: (child.text or "").strip() for child in root}


def _first_sentence(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    sentence = cleaned.split(". ", 1)[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def _app_description(sentence: str, upstream_name: str) -> str:
    prefix = f"{upstream_name} is "
    if sentence.startswith(prefix):
        return sentence[len(prefix) :].rstrip(".")
    return (sentence[:1].lower() + sentence[1:]).rstrip(".") if sentence else ""


def _webui_note(values: dict[str, str]) -> str:
    for key, value in values.items():
        if key == "WebUI" and value:
            return value
    return "Open the Web UI from the Unraid Docker page after the container starts."


def _catalog_branch(repo: str | None, icon_only: bool) -> str:
    if repo:
        suffix = "icons" if icon_only else "catalog-assets"
        return f"sync-awesome-unraid/{repo}-{suffix}"
    return "sync-awesome-unraid/fleet-catalog-assets"


def _catalog_title(repo: str | None, icon_only: bool) -> str:
    if repo:
        target = "icons" if icon_only else "catalog assets"
        return f"ci(sync): sync {repo} {target}"
    return "ci(sync): sync fleet catalog assets"


def _catalog_body(repo: str | None, icon_only: bool) -> str:
    scope = f"`{repo}` " if repo else "fleet "
    mode = "icon-only staged launch sync" if icon_only else "catalog XML/icon sync"
    body = f"""## Summary
- Syncs {scope}assets into `awesome-unraid`.

## What changed
- Runs `aio-fleet sync-catalog`
- Mode: {mode}

## Why
- Keeps Community Apps catalog assets aligned with the source repo manifest.

## Validation
- Generated by `aio-fleet`; catalog validation should run on this PR.
"""
    assert_public_text(body, context="catalog PR body")
    return body


def _catalog_commit_and_pr(
    catalog_path: Path,
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    paths: list[Path],
    dry_run: bool,
) -> None:
    assert_public_text(title, context="catalog PR title")
    assert_public_text(body, context="catalog PR body")
    relative_paths = [str(path.relative_to(catalog_path)) for path in paths]
    commands = [
        ["git", "config", "user.name", "github-actions[bot]"],
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        ["git", "checkout", "-B", branch],
        ["git", "add", *relative_paths],
        ["git", "commit", "-m", title],
        ["git", "push", "-u", "origin", branch],
    ]
    for command in commands:
        if dry_run:
            print(f"would run {' '.join(command)}")
            continue
        result = _run(command, cwd=catalog_path)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode != 0:
            raise RuntimeError(f"catalog command failed: {' '.join(command)}")

    if dry_run:
        print(f"would open or update PR {branch} -> {base}")
        return

    existing = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            base,
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
        ],
        cwd=catalog_path,
    )
    number = existing.stdout.strip()
    if number:
        result = _run(
            ["gh", "pr", "edit", number, "--title", title, "--body", body],
            cwd=catalog_path,
        )
    else:
        result = _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=catalog_path,
        )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        raise RuntimeError("catalog PR command failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aio-fleet")
    parser.add_argument("--manifest", default="fleet.yml")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--repo", action="append")
    doctor.add_argument("--github", action="store_true")
    doctor.add_argument("--policy", default="infra/github/github-policy.yml")
    doctor.add_argument("--check-secrets", action="store_true")
    doctor.add_argument("--no-local", action="store_true")
    doctor.add_argument("--no-manifest-checks", action="store_true")
    doctor.add_argument("--app-checks", action="store_true")
    doctor.add_argument("--publish", action="store_true")
    doctor.add_argument("--cleanup", action="store_true")
    doctor.add_argument("--alerts", action="store_true")
    doctor.add_argument("--require-alerts", action="store_true")
    doctor.add_argument("--live-auth", action="store_true")
    doctor.add_argument("--check-delete-scope", action="store_true")
    doctor.add_argument("--format", choices=["text", "json"], default="text")
    doctor.set_defaults(func=cmd_doctor)
    status = sub.add_parser("status")
    status.add_argument("--github", action="store_true")
    status.add_argument("--policy", default="infra/github/github-policy.yml")
    status.add_argument("--catalog-path")
    status.set_defaults(func=cmd_status)

    debt = sub.add_parser("debt-report")
    debt.add_argument("--catalog-path")
    debt.add_argument("--github", action="store_true")
    debt.add_argument("--policy", default="infra/github/github-policy.yml")
    debt.add_argument("--trunk", action="store_true")
    debt.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    debt.set_defaults(func=cmd_debt_report)
    standards = sub.add_parser("standards")
    standards_sub = standards.add_subparsers(dest="standards_command", required=True)
    standards_reconcile = standards_sub.add_parser("reconcile")
    standards_reconcile.add_argument("--repo", action="append")
    standards_reconcile.add_argument("--github", action="store_true")
    standards_reconcile.add_argument(
        "--policy", default="infra/github/github-policy.yml"
    )
    standards_reconcile.add_argument("--release", action="store_true")
    standards_reconcile.add_argument("--registry", action="store_true")
    standards_reconcile.add_argument(
        "--write",
        action="store_true",
        help="Apply safe local fixes for generated app manifests and retired shared paths.",
    )
    standards_reconcile.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview reconcile actions without applying --write changes.",
    )
    standards_reconcile.add_argument(
        "--allow-drift",
        action="store_true",
        help="Return success while still reporting drift.",
    )
    standards_reconcile.add_argument(
        "--format", choices=["text", "json"], default="json"
    )
    standards_reconcile.set_defaults(func=cmd_standards_reconcile)

    sync_catalog = sub.add_parser("sync-catalog")
    sync_catalog.add_argument("--repo")
    sync_catalog.add_argument("--repo-path")
    sync_catalog.add_argument("--catalog-path", required=True)
    sync_catalog.add_argument("--icon-only", action="store_true")
    sync_catalog.add_argument("--create-pr", action="store_true")
    sync_catalog.add_argument("--branch")
    sync_catalog.add_argument("--base", default="main")
    sync_catalog.add_argument("--title")
    sync_catalog.add_argument("--body")
    sync_catalog.add_argument("--dry-run", action="store_true")
    sync_catalog.set_defaults(func=cmd_sync_catalog)

    actions = sub.add_parser("validate-actions")
    actions.add_argument("--repo-path", default=".")
    actions.set_defaults(func=cmd_validate_actions)

    caller = sub.add_parser("verify-caller")
    caller.add_argument("--repo", required=True)
    caller.add_argument("--repo-path", required=True)
    caller.set_defaults(func=cmd_verify_caller)

    derived = sub.add_parser("validate-derived")
    derived.add_argument("--repo-path", default=".")
    derived.add_argument("--strict-placeholders", action="store_true")
    derived.add_argument("--template-xml")
    derived.set_defaults(func=cmd_validate_derived)

    common_template = sub.add_parser("validate-template-common")
    common_template.add_argument("--repo")
    common_template.add_argument("--repo-path")
    common_template.add_argument("--all", action="store_true")
    common_template.set_defaults(func=cmd_validate_template_common)

    repo = sub.add_parser("validate-repo")
    repo.add_argument("--repo", required=True)
    repo.add_argument("--repo-path", default=".")
    repo.set_defaults(func=cmd_validate_repo)

    catalog = sub.add_parser("validate-catalog")
    catalog.add_argument("--catalog-path", required=True)
    catalog.set_defaults(func=cmd_validate_catalog)

    catalog_audit = sub.add_parser("catalog-audit")
    catalog_audit.add_argument("--catalog-path", required=True)
    catalog_audit.add_argument(
        "--format", choices=["text", "json", "markdown"], default="text"
    )
    catalog_audit.set_defaults(func=cmd_catalog_audit)

    catalog_changelog = sub.add_parser("catalog-changelog")
    catalog_changelog.add_argument("--catalog-path", required=True)
    catalog_changelog.add_argument("--write", action="store_true")
    catalog_changelog.add_argument("--check", action="store_true")
    catalog_changelog.set_defaults(func=cmd_catalog_changelog)

    catalog_workflow = sub.add_parser("catalog-workflow")
    catalog_workflow.add_argument("--catalog-path", required=True)
    catalog_workflow.add_argument("--aio-fleet-ref")
    catalog_workflow.add_argument("--write", action="store_true")
    catalog_workflow.add_argument("--check", action="store_true")
    catalog_workflow.set_defaults(func=cmd_catalog_workflow)

    github = sub.add_parser("validate-github")
    github.add_argument("--policy", default="infra/github/github-policy.yml")
    github.add_argument("--repo", action="append")
    github.add_argument("--check-secrets", action="store_true")
    github.set_defaults(func=cmd_validate_github)

    check = sub.add_parser("check")
    check_sub = check.add_subparsers(dest="check_command", required=True)
    check_run = check_sub.add_parser("run")
    check_run.add_argument("--repo", required=True)
    check_run.add_argument("--sha", required=True)
    check_run.add_argument(
        "--event",
        required=True,
        choices=["pull_request", "push", "release", "workflow_dispatch"],
    )
    check_run.add_argument(
        "--status", choices=["queued", "in_progress", "completed"], default="completed"
    )
    check_run.add_argument(
        "--conclusion",
        choices=[
            "action_required",
            "cancelled",
            "failure",
            "neutral",
            "skipped",
            "stale",
            "success",
            "timed_out",
        ],
    )
    check_run.add_argument("--summary", default="")
    check_run.add_argument("--details-url")
    check_run.add_argument("--dry-run", action="store_true")
    check_run.set_defaults(func=cmd_check_run)

    alert = sub.add_parser("alert")
    alert_sub = alert.add_subparsers(dest="alert_command", required=True)
    alert_send = alert_sub.add_parser("send")
    alert_send.add_argument("--event", required=True)
    alert_send.add_argument(
        "--status",
        choices=[
            "auto",
            "success",
            "failure",
            "warning",
            "info",
            "cancelled",
            "skipped",
        ],
        default="auto",
    )
    alert_send.add_argument("--summary", default="")
    alert_send.add_argument("--repo")
    alert_send.add_argument("--component")
    alert_send.add_argument("--dedupe-key")
    alert_send.add_argument("--details-url")
    alert_send.add_argument("--annotation", action="append")
    alert_send.add_argument("--failure-file", action="append")
    alert_send.add_argument("--report-json")
    alert_send.add_argument("--kuma-url")
    alert_send.add_argument("--webhook-url")
    alert_send.add_argument(
        "--webhook-format", choices=["json", "text"], default="json"
    )
    alert_send.add_argument("--force-webhook", action="store_true")
    alert_send.add_argument("--dry-run", action="store_true")
    alert_send.add_argument("--format", choices=["text", "json"], default="text")
    alert_send.set_defaults(func=cmd_alert_send)
    alert_doctor = alert_sub.add_parser("doctor")
    alert_doctor.add_argument("--kuma-url")
    alert_doctor.add_argument("--webhook-url")
    alert_doctor.add_argument("--require-alerts", action="store_true")
    alert_doctor.add_argument("--format", choices=["text", "json"], default="text")
    alert_doctor.set_defaults(func=cmd_alert_doctor)
    alert_test = alert_sub.add_parser("test")
    alert_test.add_argument("--event", default="upstream-update")
    alert_test.add_argument(
        "--status",
        choices=["success", "failure", "warning"],
        default="warning",
    )
    alert_test.add_argument("--summary", default="aio-fleet alert test")
    alert_test.add_argument("--repo")
    alert_test.add_argument("--component")
    alert_test.add_argument("--dedupe-key")
    alert_test.add_argument("--details-url")
    alert_test.add_argument("--kuma-url")
    alert_test.add_argument("--webhook-url")
    alert_test.add_argument(
        "--webhook-format", choices=["json", "text"], default="json"
    )
    alert_test.add_argument("--dry-run", action="store_true")
    alert_test.add_argument("--format", choices=["text", "json"], default="text")
    alert_test.set_defaults(func=cmd_alert_test)

    dashboard = sub.add_parser("fleet-dashboard")
    dashboard_sub = dashboard.add_subparsers(
        dest="fleet_dashboard_command", required=True
    )
    dashboard_update = dashboard_sub.add_parser("update")
    dashboard_update.add_argument("--issue-repo", default="wgross19/aio-fleet")
    dashboard_update.add_argument("--issue-number", type=int)
    dashboard_update.add_argument("--write", action="store_true")
    dashboard_update.add_argument("--dry-run", action="store_false", dest="write")
    dashboard_update.add_argument("--registry", action="store_true")
    dashboard_update.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    dashboard_update.add_argument("--stale-days", type=int, default=7)
    dashboard_update.add_argument("--format", choices=["text", "json"], default="text")
    dashboard_update.set_defaults(func=cmd_fleet_dashboard_update, write=False)
    dashboard_commands = dashboard_sub.add_parser("commands")
    dashboard_commands.add_argument("--issue-repo", default="wgross19/aio-fleet")
    dashboard_commands.add_argument("--issue-number", type=int, required=True)
    dashboard_commands.add_argument(
        "--format", choices=["json", "github-output"], default="json"
    )
    dashboard_commands.set_defaults(func=cmd_fleet_dashboard_commands)

    report = sub.add_parser("fleet-report")
    report_sub = report.add_subparsers(dest="fleet_report_command", required=True)
    report_generate = report_sub.add_parser("generate")
    report_generate.add_argument("--issue-repo", default="wgross19/aio-fleet")
    report_generate.add_argument("--registry", action="store_true")
    report_generate.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    report_generate.add_argument("--stale-days", type=int, default=7)
    report_generate.add_argument("--format", choices=["text", "json"], default="json")
    report_generate.set_defaults(func=cmd_fleet_report_generate)
    report_closeout = report_sub.add_parser("closeout")
    report_closeout.add_argument("--manifest", default=argparse.SUPPRESS)
    report_closeout.add_argument("--issue-repo", default="wgross19/aio-fleet")
    report_closeout.add_argument(
        "--registry",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    report_closeout.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    report_closeout.add_argument("--stale-days", type=int, default=7)
    report_closeout.add_argument("--format", choices=["text", "json"], default="json")
    report_closeout.set_defaults(func=cmd_fleet_report_closeout)
    report_schema = report_sub.add_parser("schema")
    report_schema.set_defaults(func=cmd_fleet_report_schema)
    report_validate = report_sub.add_parser("validate")
    report_validate.add_argument("--input", required=True)
    report_validate.add_argument("--format", choices=["text", "json"], default="text")
    report_validate.set_defaults(func=cmd_fleet_report_validate)
    report_explain = report_sub.add_parser("explain-run")
    report_explain.add_argument("--run-id", required=True)
    report_explain.add_argument("--issue-repo", default="wgross19/aio-fleet")
    report_explain.add_argument("--log-file", default="")
    report_explain.add_argument("--format", choices=["text", "json"], default="json")
    report_explain.set_defaults(func=cmd_fleet_report_explain_run)

    queue = sub.add_parser("fleet-queue")
    queue_sub = queue.add_subparsers(dest="fleet_queue_command", required=True)
    queue_generate = queue_sub.add_parser("generate")
    queue_generate.add_argument("--manifest", default=argparse.SUPPRESS)
    queue_generate.add_argument("--issue-repo", default="wgross19/aio-fleet")
    queue_generate.add_argument("--input", default="")
    queue_generate.add_argument("--registry", action="store_true")
    queue_generate.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    queue_generate.add_argument("--stale-days", type=int, default=7)
    queue_generate.add_argument("--format", choices=["text", "json"], default="json")
    queue_generate.set_defaults(func=cmd_fleet_queue_generate)
    queue_dispatch = queue_sub.add_parser("dispatch")
    queue_dispatch.add_argument("--id", required=True)
    queue_dispatch.add_argument("--manifest", default=argparse.SUPPRESS)
    queue_dispatch.add_argument("--issue-repo", default="wgross19/aio-fleet")
    queue_dispatch.add_argument("--input", default="")
    queue_dispatch.add_argument("--registry", action="store_true")
    queue_dispatch.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    queue_dispatch.add_argument("--stale-days", type=int, default=7)
    queue_dispatch.add_argument(
        "--dry-run", action=argparse.BooleanOptionalAction, default=True
    )
    queue_dispatch.add_argument("--format", choices=["text", "json"], default="json")
    queue_dispatch.set_defaults(func=cmd_fleet_queue_dispatch)

    fleetbot = sub.add_parser("fleetbot")
    fleetbot_sub = fleetbot.add_subparsers(dest="fleetbot_command", required=True)
    fleetbot_render = fleetbot_sub.add_parser("render-command")
    fleetbot_render.add_argument("--command", dest="command_name", required=True)
    fleetbot_render.add_argument("--repo", default="")
    fleetbot_render.add_argument("--run-id", default="")
    fleetbot_render.add_argument("--manifest", default=argparse.SUPPRESS)
    fleetbot_render.add_argument("--issue-repo", default="wgross19/aio-fleet")
    fleetbot_render.add_argument("--input", default="")
    fleetbot_render.add_argument("--registry", action="store_true")
    fleetbot_render.add_argument(
        "--include-activity",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    fleetbot_render.add_argument("--stale-days", type=int, default=7)
    fleetbot_render.add_argument("--format", choices=["text", "json"], default="json")
    fleetbot_render.set_defaults(func=cmd_fleetbot_render_command)

    poll = sub.add_parser("poll")
    poll.add_argument("--no-prs", action="store_true")
    poll.add_argument("--no-main", action="store_true")
    poll.add_argument("--create-checks", action="store_true")
    poll.add_argument("--missing-checks-only", action="store_true")
    poll.add_argument("--dry-run", action="store_true")
    poll.add_argument("--format", choices=["text", "json"], default="text")
    poll.set_defaults(func=cmd_poll)

    control = sub.add_parser("control-check")
    control.add_argument("--repo", required=True)
    control.add_argument("--repo-path")
    control.add_argument("--sha", required=True)
    control.add_argument("--source", default="")
    control.add_argument(
        "--event",
        required=True,
        choices=["pull_request", "push", "release", "workflow_dispatch"],
    )
    control.add_argument("--publish", action="store_true")
    control.add_argument(
        "--publish-component",
        action="append",
        default=[],
        dest="publish_component",
        help="Publish only this image component; may be provided more than once.",
    )
    control.add_argument("--no-trunk", action="store_true")
    control.add_argument("--no-integration", action="store_true")
    control.add_argument("--no-github-prereleases", action="store_true")
    control.add_argument(
        "--validation-only",
        action="store_true",
        help="Run app validation with publish-context checks but skip publish steps.",
    )
    control.add_argument(
        "--publish-only",
        action="store_true",
        help="Run trusted publish steps after app validation has already passed.",
    )
    control.add_argument("--check-run", action="store_true")
    control.add_argument("--dry-run", action="store_true")
    control.add_argument("--report-json")
    control.add_argument(
        "--changed-paths-json",
        default="",
        help="JSON array of changed paths to classify before running full checks.",
    )
    control.add_argument(
        "--changed-files-json",
        default="",
        help="JSON array of changed file path/status objects for cleanup classification.",
    )
    control.add_argument(
        "--resolve-changed-files",
        action="store_true",
        help="Resolve changed file path/status metadata from GitHub for the target event.",
    )
    control.add_argument(
        "--no-fast-path",
        action="store_true",
        help="Disable cleanup-only fast-path classification.",
    )
    control.add_argument(
        "--fast-path-only",
        action="store_true",
        help="Fail instead of running full validation when changed paths are not cleanup-only.",
    )
    control.add_argument("--transaction-id", default="")
    control.set_defaults(func=cmd_control_check)

    registry = sub.add_parser("registry")
    registry_sub = registry.add_subparsers(dest="registry_command", required=True)
    registry_preflight = registry_sub.add_parser("preflight")
    registry_preflight.add_argument(
        "--mode",
        action="append",
        choices=["all", "publish", "cleanup"],
        default=[],
        help="Preflight surface to check; defaults to all.",
    )
    registry_preflight.add_argument("--repo")
    registry_preflight.add_argument("--repo-path")
    registry_preflight.add_argument("--component", default="aio")
    registry_preflight.add_argument("--image")
    registry_preflight.add_argument(
        "--offline",
        action="store_false",
        dest="live_auth",
        help="Only check local credential presence; skip Docker Hub API calls.",
    )
    registry_preflight.add_argument(
        "--check-delete-scope",
        action="store_true",
        help="Probe Docker Hub tag-delete permission using a random nonexistent tag.",
    )
    registry_preflight.add_argument(
        "--allow-publish-token-delete-fallback",
        action="store_true",
        help="Deprecated; cleanup always requires DOCKERHUB_DELETE_TOKEN.",
    )
    registry_preflight.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    registry_preflight.set_defaults(func=cmd_registry_preflight, live_auth=True)
    registry_verify = registry_sub.add_parser("verify")
    registry_verify.add_argument("--repo")
    registry_verify.add_argument("--repo-path")
    registry_verify.add_argument("--all", action="store_true")
    registry_verify.add_argument("--sha")
    registry_verify.add_argument("--component", default="aio")
    registry_verify.add_argument("--include-manual", action="store_true")
    registry_verify.add_argument("--dry-run", action="store_true")
    registry_verify.add_argument("--verbose", action="store_true")
    registry_verify.add_argument("--format", choices=["text", "json"], default="text")
    registry_verify.set_defaults(func=cmd_registry_verify)
    registry_publish = registry_sub.add_parser("publish")
    registry_publish.add_argument("--repo", required=True)
    registry_publish.add_argument("--repo-path")
    registry_publish.add_argument("--sha")
    registry_publish.add_argument("--component", default="aio")
    registry_publish.add_argument("--dry-run", action="store_true")
    registry_publish.add_argument(
        "--force",
        action="store_true",
        help="Push even when all expected registry tags already verify.",
    )
    registry_publish.set_defaults(func=cmd_registry_publish)
    registry_delete = registry_sub.add_parser("delete-dockerhub-tags")
    registry_delete.add_argument("--manifest", default=argparse.SUPPRESS)
    registry_delete.add_argument("--repo")
    registry_delete.add_argument("--repo-path")
    registry_delete.add_argument("--component", default="aio")
    registry_delete.add_argument("--image")
    registry_delete.add_argument("--tag", action="append", default=[])
    registry_delete.add_argument("--tag-list", default="")
    registry_delete.add_argument("--required-substring", default="")
    registry_delete.add_argument("--dry-run", action="store_true")
    registry_delete.add_argument("--format", choices=["text", "json"], default="text")
    registry_delete.set_defaults(func=cmd_registry_delete_dockerhub_tags)

    upstream = sub.add_parser("upstream")
    upstream_sub = upstream.add_subparsers(dest="upstream_command", required=True)
    upstream_monitor = upstream_sub.add_parser("monitor")
    upstream_monitor.add_argument("--repo")
    upstream_monitor.add_argument("--repo-path")
    upstream_monitor.add_argument("--all", action="store_true")
    upstream_monitor.add_argument("--include-manual", action="store_true")
    upstream_monitor.add_argument("--write", action="store_true")
    upstream_monitor.add_argument("--create-pr", action="store_true")
    upstream_monitor.add_argument("--post-check", action="store_true")
    upstream_monitor.add_argument("--dry-run", action="store_true")
    upstream_monitor.add_argument("--format", choices=["text", "json"], default="text")
    upstream_monitor.set_defaults(func=cmd_upstream_monitor)
    upstream_assess = upstream_sub.add_parser("assess")
    upstream_assess.add_argument("--repo", required=True)
    upstream_assess.add_argument("--repo-path")
    upstream_assess.add_argument("--pr", type=int)
    upstream_assess.add_argument("--branch")
    upstream_assess.add_argument("--format", choices=["text", "json"], default="text")
    upstream_assess.set_defaults(func=cmd_upstream_assess)

    release = sub.add_parser("release")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_status = release_sub.add_parser("status")
    release_status.add_argument("--repo", required=True)
    release_status.add_argument("--component", default="aio")
    release_status.add_argument("--repo-path")
    release_status.add_argument("--format", choices=["text", "json"], default="text")
    release_status.set_defaults(func=cmd_release_status)
    release_plan = release_sub.add_parser("plan")
    release_plan.add_argument("--repo")
    release_plan.add_argument("--component")
    release_plan.add_argument("--repo-path")
    release_plan.add_argument("--all", action="store_true")
    release_plan.add_argument("--registry", action="store_true")
    release_plan.add_argument("--registry-verify-attempts", type=int, default=8)
    release_plan.add_argument("--format", choices=["text", "json"], default="text")
    release_plan.set_defaults(func=cmd_release_plan)
    release_reconcile = release_sub.add_parser("reconcile")
    release_reconcile.add_argument("--input")
    release_reconcile.add_argument("--repo")
    release_reconcile.add_argument("--component")
    release_reconcile.add_argument("--repo-path")
    release_reconcile.add_argument("--all", action="store_true")
    release_reconcile.add_argument("--registry", action="store_true")
    release_reconcile.add_argument("--create-upstream-prs", action="store_true")
    release_reconcile.add_argument("--write", action="store_true")
    release_reconcile.add_argument("--dry-run", action="store_true")
    release_reconcile.add_argument("--post-check", action="store_true")
    release_reconcile.add_argument("--format", choices=["text", "json"], default="json")
    release_reconcile.set_defaults(func=cmd_release_reconcile)
    release_preflight = release_sub.add_parser("preflight")
    release_preflight.add_argument("--repo", required=True)
    release_preflight.add_argument("--component")
    release_preflight.add_argument("--repo-path")
    release_preflight.add_argument("--sha", default="")
    release_preflight.add_argument(
        "--event",
        choices=["pull_request", "push", "release", "workflow_dispatch"],
        default="push",
    )
    release_preflight.add_argument("--mode", default="transaction")
    release_preflight.add_argument("--write", action="store_true")
    release_preflight.add_argument("--require-credentials", action="store_true")
    release_preflight.add_argument("--required-checks-passed", action="store_true")
    release_preflight.add_argument("--format", choices=["text", "json"], default="text")
    release_preflight.set_defaults(func=cmd_release_preflight)
    release_transaction = release_sub.add_parser("transaction")
    release_transaction.add_argument("--repo")
    release_transaction.add_argument("--component")
    release_transaction.add_argument("--repo-path")
    release_transaction.add_argument("--sha", default="")
    release_transaction.add_argument(
        "--event",
        choices=["pull_request", "push", "release", "workflow_dispatch"],
        default="push",
    )
    release_transaction.add_argument("--dry-run", action="store_true")
    release_transaction.add_argument("--write", action="store_true")
    release_transaction.add_argument("--require-credentials", action="store_true")
    release_transaction.add_argument("--required-checks-passed", action="store_true")
    release_transaction.add_argument("--transaction-id", default="")
    release_transaction.add_argument("--report-json")
    release_transaction.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    release_transaction.set_defaults(func=cmd_release_transaction)
    release_transaction_sub = release_transaction.add_subparsers(
        dest="transaction_command"
    )
    release_transaction_resume = release_transaction_sub.add_parser("resume")
    release_transaction_resume.add_argument("--id", required=True)
    release_transaction_resume.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    release_transaction_resume.set_defaults(func=cmd_release_transaction_resume)
    release_prepare = release_sub.add_parser("prepare")
    release_prepare.add_argument("--repo", required=True)
    release_prepare.add_argument("--component", default="aio")
    release_prepare.add_argument("--repo-path")
    release_prepare.add_argument("--dry-run", action="store_true")
    release_prepare.add_argument("--github-output")
    release_prepare.set_defaults(func=cmd_release_prepare)
    release_publish = release_sub.add_parser("publish")
    release_publish.add_argument("--repo", required=True)
    release_publish.add_argument("--component", default="aio")
    release_publish.add_argument("--repo-path")
    release_publish.add_argument("--dry-run", action="store_true")
    release_publish.add_argument("--report-json")
    release_publish.add_argument("--format", choices=["text", "json"], default="text")
    release_publish.set_defaults(func=cmd_release_publish)
    release_publish_prereleases = release_sub.add_parser("publish-github-prereleases")
    release_publish_prereleases.add_argument("--repo", required=True)
    release_publish_prereleases.add_argument("--component", action="append", default=[])
    release_publish_prereleases.add_argument("--repo-path")
    release_publish_prereleases.add_argument("--dry-run", action="store_true")
    release_publish_prereleases.add_argument("--control-report-json")
    release_publish_prereleases.add_argument("--expected-sha")
    release_publish_prereleases.set_defaults(
        func=cmd_release_publish_github_prereleases
    )

    readiness = sub.add_parser("release-readiness")
    readiness.add_argument("--repo", required=True)
    readiness.add_argument("--component", default="aio")
    readiness.add_argument("--catalog-path")
    readiness.add_argument("--policy", default="infra/github/github-policy.yml")
    readiness.add_argument("--format", choices=["text", "json"], default="text")
    readiness.set_defaults(func=cmd_release_readiness)

    onboard = sub.add_parser("onboard-repo")
    onboard.add_argument("--repo", required=True)
    onboard.add_argument(
        "--mode",
        choices=["existing", "new-from-template", "rehab"],
        default="existing",
    )
    onboard.add_argument(
        "--profile",
        default="changelog-version",
        choices=[
            "template",
            "upstream-aio-track",
            "changelog-version",
            "dify",
            "multi-component",
            "signoz-suite",
        ],
    )
    onboard.add_argument(
        "--shape",
        choices=[
            "single-image",
            "multi-component",
            "submodule-backed",
            "destination-only",
            "rehab-only",
        ],
    )
    onboard.add_argument("--upstream-name")
    onboard.add_argument("--image-name")
    onboard.add_argument("--local-path-base", default="<local-checkout-path>")
    onboard.add_argument("--dry-run", action="store_true")
    onboard.add_argument("--format", choices=["text", "json"], default="text")
    onboard.set_defaults(func=cmd_onboard_repo)

    promote = sub.add_parser("promote-rehab")
    promote.add_argument("--repo", required=True)
    promote.add_argument(
        "--profile",
        default="changelog-version",
        choices=[
            "upstream-aio-track",
            "changelog-version",
            "dify",
            "multi-component",
            "signoz-suite",
        ],
    )
    promote.add_argument("--dry-run", action="store_true")
    promote.add_argument("--format", choices=["text", "json"], default="text")
    promote.set_defaults(func=cmd_promote_rehab)

    export_app_manifest = sub.add_parser("export-app-manifest")
    export_app_manifest.add_argument("--repo", required=True)
    export_app_manifest.add_argument("--output")
    export_app_manifest.add_argument("--write", action="store_true")
    export_app_manifest.set_defaults(func=cmd_export_app_manifest)

    import_app_manifest = sub.add_parser("import-app-manifest")
    import_app_manifest.add_argument("--path", required=True)
    import_app_manifest.set_defaults(func=cmd_import_app_manifest)

    infra = sub.add_parser("infra")
    infra_sub = infra.add_subparsers(dest="infra_command", required=True)
    infra_doctor = infra_sub.add_parser("doctor")
    infra_doctor.add_argument("--path", default="infra/github")
    infra_doctor.add_argument("--policy", default="infra/github/github-policy.yml")
    infra_doctor.add_argument("--skip-tofu", action="store_true")
    infra_doctor.set_defaults(func=cmd_infra_doctor)

    signing = sub.add_parser("signing")
    signing_sub = signing.add_subparsers(dest="signing_command", required=True)
    signing_doctor = signing_sub.add_parser("doctor")
    signing_doctor.add_argument("--repo", action="append")
    signing_doctor.add_argument("--all", action="store_true")
    signing_doctor.add_argument("--no-hooks", action="store_true")
    signing_doctor.add_argument("--format", choices=["text", "json"], default="text")
    signing_doctor.set_defaults(func=cmd_signing_doctor)

    support = sub.add_parser("support-thread")
    support_sub = support.add_subparsers(dest="support_command", required=True)
    support_render = support_sub.add_parser("render")
    support_render.add_argument("--repo", required=True)
    support_render.set_defaults(func=cmd_support_thread_render)

    validate = sub.add_parser("validate")
    validate.add_argument("--all", action="store_true")
    validate.add_argument("--repo")
    validate.set_defaults(func=cmd_validate)

    cleanup = sub.add_parser("cleanup-repo")
    cleanup.add_argument("--repo")
    cleanup.add_argument("--repo-path")
    cleanup.add_argument("--all", action="store_true")
    cleanup.add_argument("--verify", action="store_true")
    cleanup.add_argument("--remove", action="store_true")
    cleanup.add_argument(
        "--fix",
        action="store_true",
        help="Alias for --remove; deletes known retired shared files.",
    )
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--format", choices=["text", "json"], default="text")
    cleanup.set_defaults(func=cmd_cleanup_repo)

    security = sub.add_parser("security")
    security_sub = security.add_subparsers(dest="security_command", required=True)
    security_audit = security_sub.add_parser("audit-workflows")
    security_audit.add_argument("--path", default=".")
    security_audit.add_argument("--format", choices=["text", "json"], default="text")
    security_audit.set_defaults(func=cmd_security_audit_workflows)

    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_poll = workflow_sub.add_parser("poll-outputs")
    workflow_poll.add_argument("--input", default="poll-targets.json")
    workflow_poll.add_argument("--run-checks", action="store_true")
    workflow_poll.add_argument("--github-output")
    workflow_poll.set_defaults(func=cmd_workflow_poll_outputs)
    workflow_upstream_poll = workflow_sub.add_parser("upstream-poll-targets")
    workflow_upstream_poll.add_argument("--manifest", default="fleet.yml")
    workflow_upstream_poll.add_argument("--input", default="upstream-report.json")
    workflow_upstream_poll.add_argument("--github-output")
    workflow_upstream_poll.set_defaults(func=cmd_workflow_upstream_poll_targets)
    workflow_upstream_summary = workflow_sub.add_parser("upstream-summary")
    workflow_upstream_summary.add_argument("--input", default="upstream-report.json")
    workflow_upstream_summary.add_argument("--output")
    workflow_upstream_summary.set_defaults(func=cmd_workflow_upstream_summary)
    workflow_registry_summary = workflow_sub.add_parser("registry-summary")
    workflow_registry_summary.add_argument("--input", default="registry-report.json")
    workflow_registry_summary.add_argument("--status", default="")
    workflow_registry_summary.add_argument("--output")
    workflow_registry_summary.set_defaults(func=cmd_workflow_registry_summary)
    workflow_dashboard_checkout = workflow_sub.add_parser("checkout-dashboard")
    workflow_dashboard_checkout.add_argument(
        "--checkout-root", default="dashboard-checkouts"
    )
    workflow_dashboard_checkout.add_argument(
        "--output-manifest", default="fleet-dashboard.manifest.yml"
    )
    workflow_dashboard_checkout.add_argument("--token")
    workflow_dashboard_checkout.set_defaults(func=cmd_workflow_checkout_dashboard)
    workflow_upstream_checkout = workflow_sub.add_parser("checkout-upstream")
    workflow_upstream_checkout.add_argument(
        "--checkout-root", default="upstream-checkouts"
    )
    workflow_upstream_checkout.add_argument("--manifest", default=argparse.SUPPRESS)
    workflow_upstream_checkout.add_argument(
        "--output-manifest", default="upstream-monitor.manifest.yml"
    )
    workflow_upstream_checkout.add_argument("--output")
    workflow_upstream_checkout.add_argument("--token")
    workflow_upstream_checkout.set_defaults(func=cmd_workflow_checkout_upstream)
    workflow_upstream = workflow_sub.add_parser("upstream-monitor")
    workflow_upstream.add_argument("--manifest", default=argparse.SUPPRESS)
    workflow_upstream.add_argument("--output", default="upstream-report.json")
    workflow_upstream.add_argument("--mutate", action="store_true")
    workflow_upstream.add_argument("--dry-run", action="store_true")
    workflow_upstream.set_defaults(func=cmd_workflow_upstream_monitor)
    workflow_upstream_validate = workflow_sub.add_parser("upstream-validate")
    workflow_upstream_validate.add_argument("--manifest", default=argparse.SUPPRESS)
    workflow_upstream_validate.add_argument(
        "--checkout-root", default="upstream-checkouts"
    )
    workflow_upstream_validate.add_argument("--input", default="upstream-report.json")
    workflow_upstream_validate.add_argument("--output", default="upstream-report.json")
    workflow_upstream_validate.set_defaults(func=cmd_workflow_upstream_validate)
    workflow_upstream_actions = workflow_sub.add_parser("upstream-actions")
    workflow_upstream_actions.add_argument("--manifest", default=argparse.SUPPRESS)
    workflow_upstream_actions.add_argument(
        "--checkout-root", default="upstream-checkouts"
    )
    workflow_upstream_actions.add_argument("--input", default="upstream-report.json")
    workflow_upstream_actions.add_argument("--output", default="upstream-report.json")
    workflow_upstream_actions.set_defaults(func=cmd_workflow_upstream_actions)
    workflow_registry = workflow_sub.add_parser("registry-audit")
    workflow_registry.add_argument("--checkout-root", default="registry-checkouts")
    workflow_registry.add_argument("--output", default="registry-report.json")
    workflow_registry.add_argument("--token")
    workflow_registry.add_argument("--github-output")
    workflow_registry.set_defaults(func=cmd_workflow_registry_audit)
    workflow_smoke = workflow_sub.add_parser("smoke-test")
    workflow_smoke.add_argument("--arch", required=True, choices=["amd64", "arm64"])
    workflow_smoke.add_argument("--output", default="smoke-report.json")
    workflow_smoke.add_argument("--github-output")
    workflow_smoke.set_defaults(func=cmd_workflow_smoke_test)
    workflow_control_report = workflow_sub.add_parser("control-report")
    workflow_control_report.add_argument("--repo", required=True)
    workflow_control_report.add_argument("--sha", required=True)
    workflow_control_report.add_argument(
        "--event",
        required=True,
        choices=["pull_request", "push", "release", "workflow_dispatch"],
    )
    workflow_control_report.add_argument("--source", default="")
    workflow_control_report.add_argument("--publish", action="store_true")
    workflow_control_report.add_argument(
        "--publish-component",
        action="append",
        default=[],
        dest="publish_component",
    )
    workflow_control_report.add_argument("--failure", action="append", default=[])
    workflow_control_report.add_argument(
        "--status", choices=["success", "failure"], default=""
    )
    workflow_control_report.add_argument("--output")
    workflow_control_report.add_argument("--transaction-id", default="")
    workflow_control_report.add_argument(
        "--check-mode",
        choices=[CHECK_MODE_FULL, CHECK_MODE_FAST_CLEANUP],
        default=CHECK_MODE_FULL,
    )
    workflow_control_report.add_argument("--changed-paths-json", default="")
    workflow_control_report.add_argument("--changed-files-json", default="")
    workflow_control_report.add_argument("--fast-path-reason", default="")
    workflow_control_report.add_argument(
        "--format", choices=["text", "json"], default="text"
    )
    workflow_control_report.set_defaults(func=cmd_workflow_control_report)

    trunk = sub.add_parser("trunk")
    trunk_sub = trunk.add_subparsers(dest="trunk_command", required=True)
    trunk_run = trunk_sub.add_parser("run")
    trunk_run.add_argument("--repo")
    trunk_run.add_argument("--repo-path")
    trunk_run.add_argument("--all", action="store_true")
    trunk_run.add_argument("--fix", action="store_true")
    trunk_run.add_argument("--no-fix", action="store_false", dest="fix")
    trunk_run.add_argument(
        "--local",
        action="store_true",
        help="Run central Trunk config directly in the target checkout.",
    )
    trunk_run.add_argument(
        "--changed",
        action="store_false",
        dest="all_files",
        help="With --local, check only changed files instead of the full checkout.",
    )
    trunk_run.set_defaults(func=cmd_trunk_run, fix=False, all_files=True)

    trunk_audit = sub.add_parser("trunk-audit")
    trunk_audit.add_argument("--repo")
    trunk_audit.add_argument("--verbose", action="store_true")
    trunk_audit.set_defaults(func=cmd_trunk_audit)

    hooks = sub.add_parser("hooks")
    hooks_sub = hooks.add_subparsers(dest="hooks_command", required=True)
    hooks_install = hooks_sub.add_parser("install")
    hooks_install.add_argument("--repo")
    hooks_install.add_argument("--repo-path")
    hooks_install.add_argument("--all", action="store_true")
    hooks_install.add_argument("--include-destinations", action="store_true")
    hooks_install.set_defaults(func=cmd_hooks_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
