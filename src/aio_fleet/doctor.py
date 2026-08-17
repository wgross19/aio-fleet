from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from aio_fleet.checks import CHECK_NAME, upsert_check_run
from aio_fleet.manifest import FleetManifest, RepoConfig
from aio_fleet.registry import (
    _dockerhub_image_parts,
    dockerhub_auth_preflight_failure,
    dockerhub_delete_scope_preflight_failure,
)


def fleet_doctor_report(
    manifest: FleetManifest,
    *,
    repos: list[str] | None = None,
    env: dict[str, str] | None = None,
    include_local: bool = True,
    include_app_checks: bool = False,
    include_publish: bool = False,
    include_cleanup: bool = False,
    include_alerts: bool = False,
    live_auth: bool = False,
    check_delete_scope: bool = False,
    require_alerts: bool = False,
) -> dict[str, Any]:
    env = env or os.environ
    selected = _selected_repos(manifest, repos)
    checks: list[dict[str, str]] = []
    if include_local:
        for repo in selected:
            checks.extend(_repo_checkout_checks(repo))
    if include_app_checks:
        for repo in selected:
            checks.extend(_app_check_permission_checks(repo))
    if include_publish:
        checks.extend(_publish_credential_checks(env=env, live_auth=live_auth))
    if include_cleanup:
        cleanup_images = _cleanup_images(selected)
        checks.extend(
            _cleanup_credential_checks(
                env=env,
                live_auth=live_auth,
                check_delete_scope=check_delete_scope,
                images=cleanup_images,
            )
        )
    if include_alerts:
        checks.extend(_alert_config_checks(env=env, require_alerts=require_alerts))
    return _doctor_report(checks)


def manifest_shape_checks(manifest: FleetManifest) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    for repo in manifest.repos.values():
        if not repo.path.exists():
            checks.append(
                _check(
                    "manifest-repo-path",
                    "failed",
                    "checkout-missing",
                    f"{repo.name}: repo path missing",
                    repo=repo.name,
                )
            )
            continue
        for required in ("Dockerfile", "README.md", "pyproject.toml"):
            if not (repo.path / required).exists():
                checks.append(
                    _check(
                        "manifest-required-file",
                        "failed",
                        "manifest-drift",
                        f"{repo.name}: missing {required}",
                        repo=repo.name,
                    )
                )
    return checks


def _selected_repos(
    manifest: FleetManifest, repos: list[str] | None
) -> list[RepoConfig]:
    if not repos:
        return list(manifest.repos.values())
    return [manifest.repo(repo) for repo in repos]


def _repo_checkout_checks(repo: RepoConfig) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if not repo.path.exists():
        return [
            _check(
                "repo-checkout",
                "failed",
                "checkout-missing",
                f"{repo.name}: checkout path does not exist",
                repo=repo.name,
            )
        ]
    inside = _git(["rev-parse", "--is-inside-work-tree"], cwd=repo.path)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return [
            _check(
                "repo-checkout",
                "failed",
                "checkout-missing",
                f"{repo.name}: path is not a git checkout",
                repo=repo.name,
            )
        ]

    branch = _git(["branch", "--show-current"], cwd=repo.path).stdout.strip()
    if not branch:
        checks.append(
            _check(
                "repo-branch",
                "failed",
                "detached-checkout",
                f"{repo.name}: checkout is detached",
                repo=repo.name,
            )
        )
    else:
        checks.append(
            _check(
                "repo-branch",
                "ok",
                "clean-checkout",
                f"{repo.name}: branch {branch}",
                repo=repo.name,
            )
        )

    status = _git(["status", "--short"], cwd=repo.path)
    if status.returncode != 0:
        checks.append(
            _check(
                "repo-dirty",
                "failed",
                "checkout-missing",
                f"{repo.name}: git status failed",
                repo=repo.name,
            )
        )
    elif status.stdout.strip():
        checks.append(
            _check(
                "repo-dirty",
                "failed",
                "dirty-repo",
                f"{repo.name}: checkout has uncommitted or untracked files",
                repo=repo.name,
            )
        )
    else:
        checks.append(
            _check(
                "repo-dirty",
                "ok",
                "clean-checkout",
                f"{repo.name}: checkout is clean",
                repo=repo.name,
            )
        )

    upstream = _git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=repo.path
    )
    if upstream.returncode != 0:
        checks.append(
            _check(
                "repo-upstream",
                "warning",
                "branch-no-upstream",
                f"{repo.name}: branch has no upstream",
                repo=repo.name,
            )
        )
        return checks
    drift = _git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=repo.path)
    if drift.returncode != 0:
        checks.append(
            _check(
                "repo-upstream",
                "warning",
                "branch-drift-unknown",
                f"{repo.name}: unable to inspect upstream drift",
                repo=repo.name,
            )
        )
        return checks
    ahead, behind = _ahead_behind(drift.stdout)
    if behind > 0:
        checks.append(
            _check(
                "repo-upstream",
                "failed",
                "stale-branch",
                f"{repo.name}: branch is {behind} commit(s) behind upstream",
                repo=repo.name,
            )
        )
    elif ahead > 0:
        checks.append(
            _check(
                "repo-upstream",
                "warning",
                "unpushed-branch",
                f"{repo.name}: branch is {ahead} commit(s) ahead of upstream",
                repo=repo.name,
            )
        )
    else:
        checks.append(
            _check(
                "repo-upstream",
                "ok",
                "clean-checkout",
                f"{repo.name}: branch matches upstream",
                repo=repo.name,
            )
        )
    return checks


def _app_check_permission_checks(repo: RepoConfig) -> list[dict[str, str]]:
    targets = _app_check_targets(repo)
    if not targets:
        return [
            _check(
                "app-check-permission",
                "skipped",
                "app-check-permission",
                f"{repo.name}: no main or pull request SHA available for check-run probe",
                repo=repo.name,
            )
        ]
    checks: list[dict[str, str]] = []
    for target in targets:
        sha = target["sha"]
        event = target["event"]
        source = target["source"]
        try:
            upsert_check_run(
                repo,
                sha=sha,
                event=event,
                status="completed",
                conclusion="neutral",
                summary=f"aio-fleet preflight check-run permission probe for {source}",
                name=f"{CHECK_NAME} permission probe",
            )
        except Exception as exc:
            checks.append(
                _check(
                    "app-check-permission",
                    "failed",
                    "app-check-permission",
                    f"{repo.name}:{source}: {exc}",
                    repo=repo.name,
                )
            )
        else:
            checks.append(
                _check(
                    "app-check-permission",
                    "ok",
                    "app-check-permission",
                    f"{repo.name}:{source}: check-run write probe succeeded",
                    repo=repo.name,
                )
            )
    return checks


def _publish_credential_checks(
    *, env: dict[str, str], live_auth: bool
) -> list[dict[str, str]]:
    missing = [
        key
        for key in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN", "AIO_FLEET_GHCR_TOKEN")
        if not env.get(key)
    ]
    checks = [
        _check(
            "publish-credentials",
            "failed" if missing else "ok",
            "credential-gap",
            (
                "missing " + ", ".join(missing)
                if missing
                else "Docker Hub and GHCR publish credentials are present"
            ),
        )
    ]
    if missing or not live_auth:
        checks.append(
            _check(
                "dockerhub-publish-auth",
                "skipped",
                "credential-gap",
                (
                    "live Docker Hub publish auth skipped"
                    if not missing
                    else "publish credential gaps must be fixed before live auth"
                ),
            )
        )
        return checks
    failure = dockerhub_auth_preflight_failure(
        username=str(env.get("DOCKERHUB_USERNAME", "")),
        token=str(env.get("DOCKERHUB_TOKEN", "")),
    )
    checks.append(
        _check(
            "dockerhub-publish-auth",
            "failed" if failure else "ok",
            "credential-gap",
            failure or "Docker Hub publish token accepted by /v2/auth/token",
        )
    )
    return checks


def _cleanup_credential_checks(
    *,
    env: dict[str, str],
    live_auth: bool,
    check_delete_scope: bool,
    images: list[str],
) -> list[dict[str, str]]:
    missing = [
        key
        for key in ("DOCKERHUB_USERNAME", "DOCKERHUB_DELETE_TOKEN")
        if not env.get(key)
    ]
    checks = [
        _check(
            "cleanup-credentials",
            "failed" if missing else "ok",
            "delete-scope-gap",
            (
                "missing " + ", ".join(missing)
                if missing
                else "Docker Hub delete credentials are present"
            ),
        )
    ]
    if missing or not live_auth:
        checks.append(
            _check(
                "dockerhub-cleanup-auth",
                "skipped",
                "delete-scope-gap",
                (
                    "live Docker Hub cleanup auth skipped"
                    if not missing
                    else "cleanup credential gaps must be fixed before live auth"
                ),
            )
        )
        return checks
    auth_failure = dockerhub_auth_preflight_failure(
        username=str(env.get("DOCKERHUB_USERNAME", "")),
        token=str(env.get("DOCKERHUB_DELETE_TOKEN", "")),
    )
    checks.append(
        _check(
            "dockerhub-cleanup-auth",
            "failed" if auth_failure else "ok",
            "delete-scope-gap",
            auth_failure or "Docker Hub delete token accepted by /v2/auth/token",
        )
    )
    if not check_delete_scope:
        return checks
    if not images:
        checks.append(
            _check(
                "dockerhub-delete-scope",
                "failed",
                "delete-scope-gap",
                "no Docker Hub images are available for delete-scope probing",
            )
        )
        return checks
    for image in images:
        failure = dockerhub_delete_scope_preflight_failure(
            image=image,
            username=str(env.get("DOCKERHUB_USERNAME", "")),
            token=str(env.get("DOCKERHUB_DELETE_TOKEN", "")),
        )
        checks.append(
            _check(
                "dockerhub-delete-scope",
                "failed" if failure else "ok",
                "delete-scope-gap",
                failure or f"{image}: delete-scope probe accepted",
            )
        )
    return checks


def _alert_config_checks(
    *, env: dict[str, str], require_alerts: bool
) -> list[dict[str, str]]:
    missing = [
        key
        for key in ("AIO_FLEET_KUMA_PUSH_URL", "AIO_FLEET_ALERT_WEBHOOK_URL")
        if not env.get(key)
    ]
    if not missing:
        return [
            _check(
                "alert-config",
                "ok",
                "alert-config",
                "Kuma and webhook alert destinations are configured",
            )
        ]
    return [
        _check(
            "alert-config",
            "failed" if require_alerts else "warning",
            "alert-config",
            "missing " + ", ".join(missing),
        )
    ]


def _cleanup_images(repos: list[RepoConfig]) -> list[str]:
    images: list[str] = []
    for repo in repos:
        image = repo.image_name
        if image and _dockerhub_image_parts(image) is not None:
            images.append(image)
        components = repo.raw.get("components")
        if isinstance(components, dict):
            for config in components.values():
                if not isinstance(config, dict):
                    continue
                component_image = str(config.get("image_name", "") or "")
                if (
                    component_image
                    and _dockerhub_image_parts(component_image) is not None
                ):
                    images.append(component_image)
    return list(dict.fromkeys(images))


def _app_check_targets(repo: RepoConfig) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    main = _gh(["api", f"repos/{repo.github_repo}/commits/main", "--jq", ".sha"])
    if main.returncode == 0 and main.stdout.strip():
        targets.append({"source": "main", "event": "push", "sha": main.stdout.strip()})
    prs = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--state",
            "open",
            "--json",
            "number,headRefOid,isCrossRepository",
        ]
    )
    if prs.returncode == 0 and prs.stdout.strip():
        import json

        for pr in json.loads(prs.stdout):
            if not isinstance(pr, dict) or pr.get("isCrossRepository") is True:
                continue
            sha = str(pr.get("headRefOid") or "")
            number = str(pr.get("number") or "")
            if sha:
                targets.append(
                    {"source": f"pr:{number}", "event": "pull_request", "sha": sha}
                )
    return targets


def _doctor_report(checks: list[dict[str, str]]) -> dict[str, Any]:
    failed = [check for check in checks if check["status"] == "failed"]
    warnings = [check for check in checks if check["status"] == "warning"]
    return {
        "status": "failed" if failed else "ok",
        "failure_classes": sorted(
            {check["class"] for check in failed if check.get("class")}
        ),
        "checks": checks,
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "warnings": len(warnings),
        },
    }


def _check(
    name: str,
    status: str,
    klass: str,
    detail: str,
    *,
    repo: str = "",
) -> dict[str, str]:
    payload = {"name": name, "status": status, "class": klass, "detail": detail}
    if repo:
        payload["repo"] = repo
    return payload


def _ahead_behind(value: str) -> tuple[int, int]:
    parts = value.strip().split()
    if len(parts) < 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        return subprocess.CompletedProcess(["git", *args], 127, "", "git not found")
    return subprocess.run(  # nosec B603
        [git, *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    gh = shutil.which("gh")
    if gh is None:
        return subprocess.CompletedProcess(["gh", *args], 127, "", "gh not found")
    return subprocess.run(  # nosec B603
        [gh, *args],
        check=False,
        capture_output=True,
        text=True,
    )
