from __future__ import annotations

import json
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aio_fleet.github_cli import github_cli_env
from aio_fleet.manifest import FleetManifest


@dataclass(frozen=True)
class SigningTarget:
    name: str
    path: Path
    github_repo: str
    role: str


def signing_doctor_report(
    manifest: FleetManifest,
    *,
    repos: list[str] | None = None,
    all_targets: bool = False,
    env: dict[str, str] | None = None,
    include_hooks: bool = True,
) -> dict[str, Any]:
    targets = signing_targets(manifest, repos=repos, all_targets=all_targets)
    env = env or {}
    checks: list[dict[str, str]] = []
    checks.extend(_credential_checks(env))
    known_targets = {target.name for target in targets}
    for repo in sorted(set(repos or []) - known_targets):
        checks.append(
            _check(
                "target-selection",
                "failed",
                "permission-gap",
                f"{repo}: not found in fleet manifest or dashboard destinations",
                repo=repo,
            )
        )
    for target in targets:
        checks.extend(_branch_protection_checks(target))
        checks.extend(_generated_pr_checks(target))
        checks.extend(_workflow_writer_checks(target))
        if include_hooks and target.role != "control-plane":
            checks.extend(_hook_checks(target))
    return _report(checks)


def signing_targets(
    manifest: FleetManifest, *, repos: list[str] | None, all_targets: bool
) -> list[SigningTarget]:
    selected = set(repos or [])
    targets: list[SigningTarget] = []
    seen: set[str] = set()
    for name, repo in manifest.repos.items():
        if selected and name not in selected:
            continue
        targets.append(
            SigningTarget(
                name=name,
                path=repo.path,
                github_repo=repo.github_repo,
                role="source",
            )
        )
        seen.add(name)
    destinations = manifest.raw.get("dashboard", {}).get("destination_repos", {}) or {}
    if isinstance(destinations, dict):
        for name, config in destinations.items():
            if name in seen:
                continue
            if selected and name not in selected:
                continue
            if not isinstance(config, dict):
                continue
            path = Path(str(config.get("path") or config.get("catalog_path") or ""))
            targets.append(
                SigningTarget(
                    name=name,
                    path=path,
                    github_repo=str(
                        config.get("github_repo", f"{manifest.owner}/{name}")
                    ),
                    role=str(config.get("role", "destination")),
                )
            )
            seen.add(name)
    if all_targets and (not selected or "aio-fleet" in selected):
        targets.append(
            SigningTarget(
                name="aio-fleet",
                path=manifest.path.parent,
                github_repo=f"{manifest.owner}/aio-fleet",
                role="control-plane",
            )
        )
    return targets


def _credential_checks(env: dict[str, str]) -> list[dict[str, str]]:
    missing = [
        key
        for key in (
            "AIO_FLEET_APP_INSTALLATION_ID",
            "AIO_FLEET_APP_PRIVATE_KEY",
        )
        if not env.get(key)
    ]
    if not env.get("AIO_FLEET_APP_CLIENT_ID") and not env.get("AIO_FLEET_APP_ID"):
        missing.insert(0, "AIO_FLEET_APP_CLIENT_ID or AIO_FLEET_APP_ID")
    return [
        _check(
            "fleetbot-credentials",
            "failed" if missing else "ok",
            "credential-gap" if missing else "ok",
            (
                "missing " + ", ".join(missing)
                if missing
                else "Fleetbot GitHub App credentials are present"
            ),
        )
    ]


def _branch_protection_checks(target: SigningTarget) -> list[dict[str, str]]:
    if not target.github_repo:
        return [
            _check(
                "branch-protection",
                "failed",
                "permission-gap",
                f"{target.name}: missing GitHub repository",
                repo=target.name,
            )
        ]
    result = _gh_json(
        ["api", f"repos/{target.github_repo}/branches/main/protection"], check=False
    )
    if not isinstance(result, dict):
        return [
            _check(
                "branch-protection",
                "warning",
                "branch-protection-gap",
                f"{target.name}: unable to inspect main branch protection",
                repo=target.name,
            )
        ]
    signatures = result.get("required_signatures", {})
    if isinstance(signatures, dict) and signatures.get("enabled") is True:
        return [
            _check(
                "branch-protection",
                "ok",
                "ok",
                f"{target.name}: main requires signed commits",
                repo=target.name,
            )
        ]
    return [
        _check(
            "branch-protection",
            "failed",
            "branch-protection-gap",
            f"{target.name}: main does not require signed commits",
            repo=target.name,
        )
    ]


def _generated_pr_checks(target: SigningTarget) -> list[dict[str, str]]:
    prs = open_generated_prs(target.github_repo)
    if prs is None:
        return [
            _check(
                "generated-pr-signatures",
                "warning",
                "permission-gap",
                f"{target.name}: unable to inspect open PRs",
                repo=target.name,
            )
        ]
    checks: list[dict[str, str]] = []
    if not prs:
        return [
            _check(
                "generated-pr-signatures",
                "ok",
                "ok",
                f"{target.name}: no open generated PRs",
                repo=target.name,
            )
        ]
    for pr in prs:
        number = str(pr.get("number") or "")
        signed = pr_signed_state(target.github_repo, number)
        checks.append(
            _check(
                "generated-pr-signatures",
                "ok" if signed == "verified" else "failed",
                "ok" if signed == "verified" else "unsigned-generated-pr",
                f"{target.name}#{number}: generated PR signatures are {signed}",
                repo=target.name,
            )
        )
    return checks


def generated_pr_signature_blockers(github_repo: str) -> list[str]:
    prs = open_generated_prs(github_repo)
    if prs is None:
        return [
            "unable to inspect generated PR signatures (gh authentication/api failure)"
        ]
    if not prs:
        return []
    blockers: list[str] = []
    for pr in prs:
        number = str(pr.get("number") or "")
        signed = pr_signed_state(github_repo, number)
        if signed != "verified":
            blockers.append(f"generated PR #{number} has unverified commits: {signed}")
    return blockers


def open_generated_prs(github_repo: str) -> list[dict[str, Any]] | None:
    prs = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            github_repo,
            "--state",
            "open",
            "--json",
            "number,headRefName,isCrossRepository",
        ],
        check=False,
    )
    if not isinstance(prs, list):
        return None
    return [
        pr
        for pr in prs
        if isinstance(pr, dict)
        and pr.get("isCrossRepository") is False
        and str(pr.get("headRefName", "")).startswith(("codex/", "changelog/"))
    ]


def current_generated_pr_signature_blockers(
    github_repo: str, repo_path: Path
) -> list[str]:
    branch = _git(["branch", "--show-current"], cwd=repo_path)
    if branch.returncode != 0:
        return []
    head = branch.stdout.strip()
    if not head.startswith(("codex/", "changelog/")):
        return []
    prs = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            github_repo,
            "--head",
            head,
            "--state",
            "open",
            "--json",
            "number,headRefName,isCrossRepository",
        ],
        check=False,
    )
    if not isinstance(prs, list):
        return [
            f"unable to inspect generated PR signatures for branch {head} (gh authentication/api failure)"
        ]
    if not prs:
        return []
    blockers: list[str] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        number = str(pr.get("number") or "")
        signed = pr_signed_state(github_repo, number)
        if signed != "verified":
            blockers.append(f"generated PR #{number} has unverified commits: {signed}")
    return blockers


def workflow_writer_checks(target: SigningTarget) -> list[dict[str, str]]:
    return _workflow_writer_checks(target)


def _workflow_writer_checks(target: SigningTarget) -> list[dict[str, str]]:
    workflow_dir = target.path / ".github" / "workflows"
    if not target.path.exists():
        return [
            _check(
                "automation-writers",
                "warning",
                "permission-gap",
                f"{target.name}: checkout path does not exist",
                repo=target.name,
            )
        ]
    if not workflow_dir.exists():
        return [
            _check(
                "automation-writers",
                "ok",
                "ok",
                f"{target.name}: no repo-local workflow writers",
                repo=target.name,
            )
        ]
    checks: list[dict[str, str]] = []
    for path in sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")]):
        text = path.read_text()
        if "create-pull-request" not in text:
            continue
        rel = path.relative_to(target.path).as_posix()
        signed = "sign-commits: true" in text
        app_token = "actions/create-github-app-token" in text
        client_id = "client-id:" in text and "AIO_FLEET_APP_CLIENT_ID" in text
        verified_output = "pull-request-commits-verified" in text
        verifies_existing_pr = "pull-request-number" in text
        unsafe_fallback = "|| secrets.GITHUB_TOKEN" in text
        ok = (
            signed
            and app_token
            and client_id
            and verified_output
            and verifies_existing_pr
            and not unsafe_fallback
        )
        detail = (
            "uses GitHub App signed PR commits"
            if ok
            else "must use GitHub App client-id token, sign-commits, PR-number-gated verified output check, and no GITHUB_TOKEN fallback"
        )
        checks.append(
            _check(
                "automation-writers",
                "ok" if ok else "failed",
                "ok" if ok else "external-writer-gap",
                f"{target.name}:{rel}: {detail}",
                repo=target.name,
            )
        )
    if not checks:
        checks.append(
            _check(
                "automation-writers",
                "ok",
                "ok",
                f"{target.name}: no repo-local create-pull-request writers",
                repo=target.name,
            )
        )
    return checks


def _hook_checks(target: SigningTarget) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    if not target.path.exists():
        return [
            _check(
                "local-hooks",
                "warning",
                "permission-gap",
                f"{target.name}: checkout path does not exist",
                repo=target.name,
            )
        ]
    hooks_path = _git(["config", "--get", "core.hooksPath"], cwd=target.path)
    expected = str(_git_dir(target.path) / "aio-fleet-hooks")
    if hooks_path.returncode == 0 and hooks_path.stdout.strip() == expected:
        checks.append(
            _check(
                "local-hooks",
                "ok",
                "ok",
                f"{target.name}: aio-fleet hooks installed",
                repo=target.name,
            )
        )
    else:
        checks.append(
            _check(
                "local-hooks",
                "warning",
                "external-writer-gap",
                f"{target.name}: aio-fleet hooks are not installed",
                repo=target.name,
            )
        )
    trunk_dir = target.path / ".trunk"
    tracked = _git(["ls-files", "--", ".trunk"], cwd=target.path)
    if tracked.stdout.strip():
        checks.append(
            _check(
                "local-trunk-overlay",
                "failed",
                "external-writer-gap",
                f"{target.name}: tracked .trunk overlay exists outside aio-fleet",
                repo=target.name,
            )
        )
    elif trunk_dir.exists():
        checks.append(
            _check(
                "local-trunk-overlay",
                "warning",
                "external-writer-gap",
                f"{target.name}: stray local .trunk overlay exists",
                repo=target.name,
            )
        )
    else:
        checks.append(
            _check(
                "local-trunk-overlay",
                "ok",
                "ok",
                f"{target.name}: no stray local .trunk overlay",
                repo=target.name,
            )
        )
    return checks


def pr_signed_state(github_repo: str, number: str) -> str:
    commits = _gh_json(
        ["api", f"repos/{github_repo}/pulls/{number}/commits", "--paginate"],
        check=False,
    )
    if not isinstance(commits, list) or not commits:
        return "unknown"
    reasons: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        verification = commit.get("commit", {}).get("verification", {})
        if isinstance(verification, dict) and verification.get("verified") is True:
            continue
        reasons.append(str(verification.get("reason") or "unverified"))
    return "verified" if not reasons else ",".join(sorted(set(reasons)))


def _gh_json(command: list[str], *, check: bool) -> Any:
    result = _run(["gh", *command], check=check)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        return None


def _git(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(["git", *command], cwd=cwd, check=False)


def _git_dir(repo_path: Path) -> Path:
    result = _git(["rev-parse", "--git-dir"], cwd=repo_path)
    if result.returncode != 0:
        return repo_path / ".git"
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    return git_dir.resolve()


def _run(
    command: list[str], *, cwd: Path | None = None, check: bool
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # nosec B603
        command,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        env=(
            github_cli_env(
                (
                    "AIO_FLEET_DASHBOARD_TOKEN",
                    "AIO_FLEET_UPSTREAM_TOKEN",
                    "AIO_FLEET_CHECK_TOKEN",
                    "APP_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                )
            )
            if command and command[0] == "gh"
            else None
        ),
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result


def _report(checks: list[dict[str, str]]) -> dict[str, Any]:
    failed = [check for check in checks if check["status"] == "failed"]
    warnings = [check for check in checks if check["status"] == "warning"]
    return {
        "status": "failed" if failed else "warning" if warnings else "ok",
        "failure_classes": sorted(
            {
                str(check.get("class") or check.get("classification"))
                for check in failed
                if check.get("class") or check.get("classification")
            }
        ),
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "warnings": len(warnings),
        },
        "checks": checks,
    }


def _check(
    name: str,
    status: str,
    classification: str,
    detail: str,
    *,
    repo: str = "",
) -> dict[str, str]:
    row = {
        "name": name,
        "status": status,
        "class": classification,
        "classification": classification,
        "detail": detail,
    }
    if repo:
        row["repo"] = repo
    return row
