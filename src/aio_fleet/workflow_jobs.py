from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from aio_fleet.change_scope import CHECK_MODE_FULL
from aio_fleet.changelog import component_config
from aio_fleet.control_plane import _secret_environment_key
from aio_fleet.manifest import RepoConfig, load_manifest
from aio_fleet.poll import PublishPathResolutionError, publish_components_required
from aio_fleet.upstream import (
    UpstreamMonitorResult,
    _upstream_commit_paths,
    create_or_update_upstream_pr,
    monitor_configs,
    read_arg,
    result_dict,
)


def _sanitized_subprocess_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not _secret_environment_key(key)
    }


def _upstream_monitor_subprocess_env() -> dict[str, str]:
    env = _sanitized_subprocess_env()
    env["HOME"] = tempfile.mkdtemp(prefix="aio-fleet-upstream-monitor-home-")
    return env


def _secret_environment_keys() -> list[str]:
    return sorted(key for key in os.environ if _secret_environment_key(key))


def _assert_secretless_launcher() -> None:
    unsafe = _secret_environment_keys()
    if unsafe:
        raise RuntimeError(
            "refusing to launch generator-capable upstream monitor children from "
            "a secret-bearing process: " + ", ".join(unsafe)
        )


def poll_outputs(
    *, report_path: Path, run_checks: bool, github_output: Path | None
) -> dict[str, Any]:
    payload = _read_json(report_path, default={"targets": []})
    targets = payload.get("targets", [])
    targets = targets if isinstance(targets, list) else []
    output = {
        "run_checks": run_checks,
        "has_targets": bool(targets),
        "targets": targets,
    }
    if github_output:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"run_checks={'true' if run_checks else 'false'}\n")
            handle.write(f"has_targets={'true' if targets else 'false'}\n")
            handle.write("targets<<__AIO_FLEET_TARGETS__\n")
            handle.write(json.dumps(targets, sort_keys=True))
            handle.write("\n__AIO_FLEET_TARGETS__\n")
    return output


def upstream_poll_targets(
    *, report_path: Path, manifest_path: Path, github_output: Path | None
) -> dict[str, Any]:
    """Build poll-check targets for the bot PRs the upstream monitor just opened.

    This lets the upstream-monitor run validate (and so complete the required
    check on) its own freshly-created PRs in the same control-plane run, instead
    of waiting for the best-effort hourly poll cron. Only the monitor's trusted,
    signed bot PRs are emitted, and every target is publish-disabled so the
    registry-publish gate is never reached automatically.
    """

    payload = _read_json(report_path, default={})
    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("action") == "upserted-pr":
                repo = str(node.get("repo") or "").strip()
                sha = str(node.get("sha") or "").strip()
                branch = str(node.get("branch") or "").strip()
                key = (repo, sha)
                if repo and _looks_like_sha(sha) and key not in seen:
                    seen.add(key)
                    pairs.append((repo, sha, branch))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)

    manifest = load_manifest(manifest_path)
    targets: list[dict[str, Any]] = []
    for repo_name, sha, branch in pairs:
        repo = manifest.repos.get(repo_name)
        if repo is None:
            continue
        targets.append(
            {
                "repo": repo.name,
                "sha": sha,
                "event": "pull_request",
                "source": f"upstream-pr:{branch or repo.name}",
                "checkout_submodules": bool(repo.raw.get("checkout_submodules")),
                "publish": False,
                "publish_components": [],
                "check_mode": CHECK_MODE_FULL,
                "changed_paths": [],
                "changed_files": [],
                "fast_path_reason": "",
            }
        )

    run_checks = bool(targets)
    output = {
        "run_checks": run_checks,
        "has_targets": bool(targets),
        "targets": targets,
    }
    if github_output:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"run_checks={'true' if run_checks else 'false'}\n")
            handle.write(f"has_targets={'true' if targets else 'false'}\n")
            handle.write("targets<<__AIO_FLEET_TARGETS__\n")
            handle.write(json.dumps(targets, sort_keys=True))
            handle.write("\n__AIO_FLEET_TARGETS__\n")
    return output


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(ch in "0123456789abcdef" for ch in value.lower())


def render_upstream_summary(*, report_path: Path, output_path: Path | None) -> str:
    if not report_path.exists():
        text = "No upstream report was generated.\n"
        if output_path:
            output_path.write_text(text)
        return text
    report = _read_json(report_path, default={"repos": []})
    lines = ["# Upstream Monitor", ""]
    for item in report.get("repos", []):
        if not isinstance(item, dict):
            continue
        repo = item.get("repo", "unknown")
        if item.get("skipped"):
            lines.append(f"- `{repo}`: skipped ({item['skipped']})")
            continue
        if item.get("error"):
            lines.append(f"- `{repo}`: failed ({item['error']})")
            continue
        updates = [
            result
            for result in item.get("results", [])
            if isinstance(result, dict) and result.get("updates_available")
        ]
        blocked = [
            result
            for result in item.get("results", [])
            if isinstance(result, dict)
            and (result.get("blocked") or result.get("state") == "blocked")
        ]
        state = "blocked" if blocked else "updates available" if updates else "current"
        lines.append(f"- `{repo}`: {state}")
        for result in blocked:
            lines.append(
                "  - `{component}`: {current_version} -> {latest_version} "
                "blocked: {blocked_reason}; next: {next_action}".format(**result)
            )
        for result in updates:
            if result in blocked:
                continue
            lines.append(
                f"  - `{result['component']}`: {result['current_version']} -> {result['latest_version']}"
            )
    text = "\n".join(lines) + "\n"
    if output_path:
        output_path.write_text(text)
    return text


def render_registry_summary(
    *, report_path: Path, status: str, output_path: Path | None
) -> str:
    lines = ["# Registry Audit", "", f"Verify command exit status: `{status}`", ""]
    if not report_path.exists():
        lines.extend(["Registry report was not generated.", ""])
        text = "\n".join(lines)
        if output_path:
            output_path.write_text(text)
        return text
    report = _read_json(report_path, default={"repos": []})
    lines.extend(
        [
            "| Repo | Component | SHA | State | Docker Hub tags | GHCR tags |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    failures: list[str] = []
    for item in report.get("repos", []):
        if not isinstance(item, dict):
            continue
        repo_failures = item.get("failures", [])
        state = (
            f"skipped:{item['skipped']}"
            if item.get("skipped")
            else "failed" if repo_failures else "ok"
        )
        sha = str(item.get("sha", ""))[:12]
        lines.append(
            "| {repo} | {component} | `{sha}` | {state} | {dockerhub} | {ghcr} |".format(
                repo=item.get("repo", ""),
                component=item.get("component", "aio"),
                sha=sha,
                state=state,
                dockerhub=len(item.get("dockerhub", [])),
                ghcr=len(item.get("ghcr", [])),
            )
        )
        for failure in repo_failures if isinstance(repo_failures, list) else []:
            failures.append(f"{item.get('repo', '')}: {failure}")
    if failures:
        lines.extend(["", "## Missing Or Failed Tags", ""])
        lines.extend(f"- `{failure}`" for failure in failures)
    else:
        lines.extend(["", "All expected registry tags resolved."])
    text = "\n".join(lines) + "\n"
    if output_path:
        output_path.write_text(text)
    return text


def checkout_dashboard_repos(
    *,
    manifest_path: Path,
    checkout_root: Path,
    output_manifest: Path,
    token: str,
) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    owner = manifest["owner"]
    checkout_root = checkout_root.resolve()
    checkout_root.mkdir(exist_ok=True)
    refs: list[tuple[str, str, Path]] = []
    for name, repo_config in manifest.get("repos", {}).items():
        path = checkout_root / name
        repo_config["path"] = str(path)
        refs.append((name, repo_config.get("github_repo", f"{owner}/{name}"), path))
    dashboard = manifest.get("dashboard", {})
    if isinstance(dashboard, dict):
        for group in ("destination_repos", "rehab_repos"):
            for name, repo_config in dashboard.get(group, {}).items():
                if not isinstance(repo_config, dict):
                    continue
                path = checkout_root / name
                repo_config["path"] = str(path)
                if repo_config.get("catalog_path"):
                    repo_config["catalog_path"] = str(path)
                refs.append(
                    (name, repo_config.get("github_repo", f"{owner}/{name}"), path)
                )
    results = _checkout_refs(refs, token=token, submodules="best-effort")
    output_manifest.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return {"repos": results, "manifest": str(output_manifest)}


def checkout_upstream_monitor_repos(
    *,
    manifest_path: Path,
    checkout_root: Path,
    output_manifest: Path,
    output_path: Path | None = None,
    token: str,
) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    owner = manifest["owner"]
    checkout_root.mkdir(exist_ok=True)
    report: dict[str, Any] = {"repos": []}
    for repo, repo_config in manifest["repos"].items():
        worktree = checkout_root / repo
        repo_config["path"] = str(worktree)
        if repo_config.get("publish_profile") == "template":
            report["repos"].append({"repo": repo, "skipped": "manual-template"})
            continue
        checkout = _checkout_refs(
            [(repo, repo_config.get("github_repo", f"{owner}/{repo}"), worktree)],
            token=token,
            submodules="required",
        )[0]
        if checkout.get("error"):
            report["repos"].append({"repo": repo, "error": checkout["error"]})
            continue
        report["repos"].append({"repo": repo, "path": str(worktree)})
    output_manifest.write_text(yaml.safe_dump(manifest, sort_keys=False))
    if output_path:
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def upstream_monitor_checkouts(
    *,
    manifest_path: Path,
    output_path: Path,
    mutate: bool,
    dry_run: bool,
) -> dict[str, Any]:
    _assert_secretless_launcher()
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    report: dict[str, Any] = {"repos": []}
    status = 0
    for repo, repo_config in manifest["repos"].items():
        if repo_config.get("publish_profile") == "template":
            report["repos"].append({"repo": repo, "skipped": "manual-template"})
            continue
        worktree_value = str(repo_config.get("path", "") or "").strip()
        if not worktree_value:
            status = 1
            report["repos"].append({"repo": repo, "error": "missing checkout path"})
            continue
        worktree = Path(worktree_value)
        if not worktree.exists():
            status = 1
            report["repos"].append(
                {"repo": repo, "error": f"checkout path does not exist: {worktree}"}
            )
            continue
        args = [
            sys.executable,
            "-m",
            "aio_fleet",
            "upstream",
            "monitor",
            "--repo",
            repo,
            "--repo-path",
            str(worktree),
            "--format",
            "json",
        ]
        if mutate:
            args.append("--write")
        if dry_run:
            args.append("--dry-run")
        run = subprocess.run(  # nosec B603
            args,
            check=False,
            text=True,
            capture_output=True,
            env=_upstream_monitor_subprocess_env(),
        )
        if run.stderr:
            print(run.stderr, file=sys.stderr, end="")
        if run.returncode != 0:
            status = 1
        try:
            parsed = json.loads(run.stdout)
        except json.JSONDecodeError:
            status = 1
            report["repos"].append(
                {
                    "repo": repo,
                    "error": (run.stdout or run.stderr).strip()
                    or "upstream monitor produced invalid JSON",
                }
            )
            continue
        report["repos"].extend(parsed.get("repos", []))
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["status"] = status
    return report


def apply_upstream_monitor_actions(
    *, manifest_path: Path, checkout_root: Path, report_path: Path, output_path: Path
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    report = _read_json(report_path, default={"repos": []})
    items = report.get("repos", [])
    items = items if isinstance(items, list) else []
    output: dict[str, Any] = {"repos": []}
    status = int(report.get("status", 0) or 0)
    for item in items:
        if not isinstance(item, dict):
            continue
        repo_name = str(item.get("repo", "") or "")
        if item.get("error") or item.get("skipped"):
            output["repos"].append(dict(item))  # type: ignore[index]
            continue
        try:
            repo = _repo_with_path(manifest.repo(repo_name), checkout_root / repo_name)
            if not repo.path.exists():
                raise ValueError(f"{repo.name}: checkout path does not exist")
            _validate_upstream_report_item(repo, item)
            updated = _append_upstream_monitor_actions(repo, [item])
            output["repos"].extend(updated)  # type: ignore[index]
        except Exception as exc:
            status = 1
            output["repos"].append(  # type: ignore[index]
                {"repo": repo_name, "error": str(exc), "partial_results": item}
            )
    output["status"] = status
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return output


def validate_upstream_monitor_report(
    *, manifest_path: Path, checkout_root: Path, report_path: Path, output_path: Path
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    report = _read_json(report_path, default={"repos": []})
    items = report.get("repos", [])
    items = items if isinstance(items, list) else []
    output: dict[str, Any] = {"repos": []}
    status = int(report.get("status", 0) or 0)
    for item in items:
        if not isinstance(item, dict):
            continue
        repo_name = str(item.get("repo", "") or "")
        if item.get("error") or item.get("skipped"):
            output["repos"].append(dict(item))  # type: ignore[index]
            continue
        try:
            repo = _repo_with_path(manifest.repo(repo_name), checkout_root / repo_name)
            if not repo.path.exists():
                raise ValueError(f"{repo.name}: checkout path does not exist")
            _validate_upstream_report_item(repo, item)
            results_payload = item.get("results", [])
            results = [
                _trusted_upstream_result_from_dict(repo, result)
                for result in results_payload
                if isinstance(result, dict)
            ]
            output["repos"].append(  # type: ignore[index]
                {
                    "repo": repo.name,
                    "results": [result_dict(result) for result in results],
                    "actions": [],
                }
            )
        except Exception as exc:
            status = 1
            output["repos"].append(  # type: ignore[index]
                {"repo": repo_name, "error": str(exc), "partial_results": item}
            )
    output["status"] = status
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
    return output


def _repo_with_path(repo: RepoConfig, path: Path) -> RepoConfig:
    raw = dict(repo.raw)
    raw["path"] = str(path)
    return RepoConfig(name=repo.name, raw=raw, defaults=repo.defaults, owner=repo.owner)


def _append_upstream_monitor_actions(
    repo: RepoConfig, repo_items: Any
) -> list[dict[str, Any]]:
    items = repo_items if isinstance(repo_items, list) else []
    updated: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("repo") != repo.name or item.get("error") or item.get("skipped"):
            updated.append(dict(item))
            continue
        results_payload = item.get("results", [])
        results = [
            _upstream_result_from_dict(repo, result)
            for result in results_payload
            if isinstance(result, dict)
        ]
        actions = (
            list(item.get("actions", []))
            if isinstance(item.get("actions"), list)
            else []
        )
        blocked = [result for result in results if result.blocked]
        writeable_updates = [
            result
            for result in results
            if result.updates_available
            and result.strategy == "pr"
            and not result.blocked
        ]
        if blocked:
            actions.append(
                {
                    "repo": repo.name,
                    "action": "skipped",
                    "reason": "blocked-upstream-update",
                    "blockers": [result_dict(result) for result in blocked],
                }
            )
        if writeable_updates:
            actions.append(
                create_or_update_upstream_pr(
                    repo,
                    results,
                    dry_run=False,
                    post_check=True,
                )
            )
        merged = dict(item)
        merged["actions"] = actions
        updated.append(merged)
    return updated


def _validate_upstream_report_item(repo: RepoConfig, item: dict[str, Any]) -> None:
    if item.get("repo") != repo.name:
        raise ValueError(f"{repo.name}: report repo mismatch")
    actions = item.get("actions", [])
    if actions not in ([], None):
        raise ValueError(f"{repo.name}: refusing untrusted child actions")
    results_payload = item.get("results", [])
    if not isinstance(results_payload, list):
        raise ValueError(f"{repo.name}: results must be a list")
    results = [
        _trusted_upstream_result_from_dict(repo, result)
        for result in results_payload
        if isinstance(result, dict)
    ]
    writeable_updates = [
        result
        for result in results
        if result.updates_available and result.strategy == "pr" and not result.blocked
    ]
    if not writeable_updates:
        return
    _validate_allowed_upstream_diff(repo, writeable_updates)
    for result in writeable_updates:
        if result.version_update:
            _assert_arg_equals(
                result.dockerfile, result.version_key, result.latest_version
            )
        if result.digest_update and result.digest_key:
            _assert_arg_equals(
                result.dockerfile, result.digest_key, result.latest_digest
            )
        config = component_config(repo, result.component)
        revision_arg = str(config.get("registry_revision_arg", "") or "").strip()
        if revision_arg and result.version_update:
            _assert_arg_equals(result.dockerfile, revision_arg, "1")


def _trusted_upstream_result_from_dict(
    repo: RepoConfig, data: dict[str, Any]
) -> UpstreamMonitorResult:
    component = str(data.get("component", "aio"))
    configs = _monitor_configs_by_component(repo)
    config = configs.get(component)
    if config is None:
        raise ValueError(f"{repo.name}: unexpected upstream component: {component}")
    expected_repo = str(data.get("repo", repo.name))
    if expected_repo != repo.name:
        raise ValueError(f"{repo.name}: result repo mismatch: {expected_repo}")
    expected_strategy = str(config.get("strategy", "notify"))
    expected_source = str(config.get("source", "manual"))
    if str(data.get("strategy", "")) != expected_strategy:
        raise ValueError(f"{repo.name}:{component}: strategy mismatch")
    if str(data.get("source", "")) != expected_source:
        raise ValueError(f"{repo.name}:{component}: source mismatch")
    dockerfile = _trusted_monitor_dockerfile(repo, config)
    reported_dockerfile = Path(str(data.get("dockerfile", "") or dockerfile))
    if reported_dockerfile != dockerfile:
        raise ValueError(f"{repo.name}:{component}: dockerfile mismatch")
    skipped_versions = data.get("skipped_versions", ())
    if not isinstance(skipped_versions, list):
        skipped_versions = []
    version_update = bool(data.get("version_update", False))
    digest_update = bool(data.get("digest_update", False))
    current_version = _safe_report_value(data.get("current_version", ""))
    latest_version = _safe_report_value(data.get("latest_version", ""))
    current_digest = _safe_report_value(data.get("current_digest", ""))
    latest_digest = _safe_report_value(data.get("latest_digest", ""))
    if version_update and current_version == latest_version:
        raise ValueError(f"{repo.name}:{component}: invalid version update")
    if digest_update and current_digest == latest_digest:
        raise ValueError(f"{repo.name}:{component}: invalid digest update")
    return UpstreamMonitorResult(
        repo=repo.name,
        component=component,
        name=str(config.get("name", component)),
        strategy=expected_strategy,
        source=expected_source,
        current_version=current_version,
        latest_version=latest_version,
        current_digest=current_digest,
        latest_digest=latest_digest,
        version_update=version_update,
        digest_update=digest_update,
        dockerfile=dockerfile,
        version_key=str(
            config.get("version_key", repo.get("upstream_version_key", ""))
        ),
        digest_key=str(config.get("digest_key", repo.get("upstream_digest_arg", ""))),
        release_notes_url=str(config.get("release_notes_url", "")),
        submodule_path=str(config.get("submodule_path", "")),
        submodule_ref=_safe_report_value(data.get("submodule_ref", "")),
        skipped_versions=tuple(
            item for item in skipped_versions if isinstance(item, dict)
        ),
        blocked_reason=str(data.get("blocked_reason", "")),
        next_action=str(data.get("next_action", "")),
    )


def _upstream_result_from_dict(
    repo: RepoConfig, data: dict[str, Any]
) -> UpstreamMonitorResult:
    return _trusted_upstream_result_from_dict(repo, data)


def _monitor_configs_by_component(repo: RepoConfig) -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for config in monitor_configs(repo):
        component = str(config.get("component", "aio"))
        if component in configs:
            raise ValueError(f"{repo.name}: duplicate upstream component: {component}")
        configs[component] = config
    return configs


def _trusted_monitor_dockerfile(repo: RepoConfig, config: dict[str, Any]) -> Path:
    relative = Path(str(config.get("dockerfile", "Dockerfile")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{repo.name}: unsafe upstream dockerfile path: {relative}")
    path = repo.path / relative
    _assert_under_repo(repo, path)
    return path


def _validate_allowed_upstream_diff(
    repo: RepoConfig, results: list[UpstreamMonitorResult]
) -> None:
    commit_paths = _safe_commit_paths(
        repo,
        _upstream_commit_paths(repo, results, repo.list_value("upstream_commit_paths")),
    )
    changed_paths = _changed_paths(repo.path)
    unexpected = sorted(changed_paths.difference(commit_paths))
    if unexpected:
        raise ValueError(
            f"{repo.name}: unexpected upstream monitor changes: "
            + ", ".join(unexpected)
        )


def _safe_commit_paths(repo: RepoConfig, paths: set[str]) -> set[str]:
    safe: set[str] = set()
    for value in paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not str(path):
            raise ValueError(f"{repo.name}: unsafe upstream commit path: {value}")
        _assert_under_repo(repo, repo.path / path)
        safe.add(path.as_posix())
    return safe


def _changed_paths(repo_path: Path) -> set[str]:
    run = subprocess.run(  # nosec B603
        [
            "git",
            "-c",
            "core.fsmonitor=",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        cwd=repo_path,
        env=_safe_git_env(),
        check=False,
        text=True,
        capture_output=True,
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip()
        raise RuntimeError(
            f"git status failed while validating upstream diff: {detail}"
        )
    paths: set[str] = set()
    for line in run.stdout.splitlines():
        if not line:
            continue
        raw = line[3:]
        if " -> " in raw:
            raise ValueError(f"refusing renamed upstream monitor path: {raw}")
        paths.add(Path(raw).as_posix())
    return paths


def _assert_arg_equals(dockerfile: Path, arg_name: str, expected: str) -> None:
    if not arg_name:
        return
    observed = read_arg(dockerfile, arg_name)
    if observed != expected:
        raise ValueError(
            f"{dockerfile.name}: expected ARG {arg_name}={expected}, got {observed}"
        )


def _safe_report_value(value: object) -> str:
    text = str(value)
    if any(char in text for char in "\r\n\0"):
        raise ValueError("upstream report value contains control characters")
    return text


def _assert_under_repo(repo: RepoConfig, path: Path) -> None:
    path.resolve().relative_to(repo.path.resolve())


def registry_audit_checkouts(
    *,
    manifest_path: Path,
    checkout_root: Path,
    output_path: Path,
    token: str,
    github_output: Path | None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    checkout_root.mkdir(exist_ok=True)
    report: dict[str, Any] = {"repos": []}
    status = 0
    for repo, repo_config in manifest.repos.items():
        if repo_config.publish_profile == "template":
            report["repos"].append(
                {
                    "repo": repo,
                    "sha": "",
                    "dockerhub": [],
                    "ghcr": [],
                    "failures": [],
                    "skipped": "manual-template-publish",
                }
            )
            continue
        worktree = checkout_root / repo
        checkout = _checkout_refs(
            [(repo, repo_config.github_repo, worktree)],
            token=token,
            submodules="none",
        )[0]
        if checkout.get("error"):
            status = 1
            report["repos"].append(
                {
                    "repo": repo,
                    "sha": "",
                    "dockerhub": [],
                    "ghcr": [],
                    "failures": [checkout["error"]],
                }
            )
            continue
        sha = subprocess.check_output(  # nosec B603 B607
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            env=_minimal_env(),
            text=True,
        ).strip()
        try:
            components = publish_components_required(repo_config, sha=sha, event="push")
        except PublishPathResolutionError as exc:
            status = 1
            report["repos"].append(
                {
                    "repo": repo,
                    "component": "repo",
                    "sha": sha,
                    "dockerhub": [],
                    "ghcr": [],
                    "failures": [str(exc)],
                }
            )
            continue
        for component in components:
            verify = subprocess.run(  # nosec B603
                [
                    sys.executable,
                    "-m",
                    "aio_fleet",
                    "registry",
                    "verify",
                    "--repo",
                    repo,
                    "--repo-path",
                    str(worktree),
                    "--sha",
                    sha,
                    "--component",
                    component,
                    "--format",
                    "json",
                ],
                env=_verify_env(),
                check=False,
                text=True,
                capture_output=True,
            )
            if verify.stderr:
                print(verify.stderr, file=sys.stderr, end="")
            if verify.returncode != 0:
                status = 1
            try:
                parsed = json.loads(verify.stdout)
            except json.JSONDecodeError:
                status = 1
                report["repos"].append(
                    {
                        "repo": repo,
                        "component": component,
                        "sha": sha,
                        "dockerhub": [],
                        "ghcr": [],
                        "failures": [
                            (verify.stdout or verify.stderr).strip()
                            or "registry verify produced invalid JSON"
                        ],
                    }
                )
                continue
            report["repos"].extend(parsed.get("repos", []))
    report["status"] = status
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if github_output:
        with github_output.open("a", encoding="utf-8") as output:
            output.write(f"status={status}\n")
    return report


def _checkout_refs(
    refs: list[tuple[str, str, Path]], *, token: str, submodules: str
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    env = _git_auth_env(token)
    seen: set[tuple[str, str]] = set()
    for name, github_repo, path in refs:
        key = (github_repo, str(path))
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(  # nosec B603 B607
            [
                "git",
                "clone",
                "--single-branch",
                "--filter=blob:none",
                f"https://github.com/{github_repo}.git",
                str(path),
            ],
            env=env,
            check=False,
            text=True,
            capture_output=True,
        )
        result: dict[str, Any] = {
            "repo": name,
            "github_repo": github_repo,
            "path": str(path),
        }
        if clone.returncode != 0:
            result["error"] = (clone.stderr or clone.stdout).strip() or "clone failed"
            results.append(result)
            continue
        if submodules != "none":
            update = subprocess.run(  # nosec B603 B607
                [
                    "git",
                    "-C",
                    str(path),
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                ],
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
            if update.returncode != 0:
                detail = (update.stderr or update.stdout).strip()
                if submodules == "required":
                    result["error"] = detail or "submodule update failed"
                else:
                    result["warning"] = detail or "submodule update skipped"
        results.append(result)
    return results


def _git_auth_env(token: str) -> dict[str, str]:
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = _minimal_env()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
        }
    )
    return env


def _minimal_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("HOME", "PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
        if key in os.environ
    }


def _safe_git_env() -> dict[str, str]:
    env = _minimal_env()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _verify_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in (
            "DOCKER_CONFIG",
            "HOME",
            "PATH",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "XDG_RUNTIME_DIR",
        )
        if key in os.environ
    }


def _publish_components(repo_config: dict[str, Any]) -> list[str]:
    components = repo_config.get("components")
    if not isinstance(components, dict):
        return ["aio"]
    names = [
        name
        for name, config in components.items()
        if name == "aio" or (isinstance(config, dict) and config.get("image_name"))
    ]
    return names or ["aio"]


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return default
    return data if isinstance(data, dict) else default
