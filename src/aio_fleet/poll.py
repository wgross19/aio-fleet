from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass

from aio_fleet.change_scope import (
    CHECK_MODE_FULL,
    classify_required_check_scope,
    publish_component_names,
    publish_components_for_changed_paths,
)
from aio_fleet.checks import check_run_satisfied
from aio_fleet.manifest import FleetManifest, RepoConfig


class PublishPathResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PollTarget:
    repo: RepoConfig
    sha: str
    event: str
    source: str
    checkout_submodules: bool = False
    publish: bool = False
    publish_components: tuple[str, ...] = ()
    check_mode: str = CHECK_MODE_FULL
    changed_paths: tuple[str, ...] = ()
    changed_files: tuple[dict[str, str], ...] = ()
    fast_path_reason: str = ""


def poll_targets(
    manifest: FleetManifest,
    *,
    include_prs: bool = True,
    include_main: bool = True,
) -> list[PollTarget]:
    targets: list[PollTarget] = []
    for repo in manifest.repos.values():
        if include_prs:
            for pull_request in _open_pull_requests(repo):
                if not _same_repository_pull_request(repo, pull_request):
                    continue
                sha = str(pull_request.get("headRefOid") or "")
                number = str(pull_request.get("number") or "")
                if sha:
                    changed_files = (
                        _pull_request_changed_files(repo, number) if number else None
                    )
                    changed_paths = _changed_file_paths(changed_files)
                    changed_file_statuses = _changed_file_statuses(changed_files)
                    scope = classify_required_check_scope(
                        repo,
                        changed_paths,
                        changed_file_statuses=changed_file_statuses,
                        publish=False,
                    )
                    targets.append(
                        PollTarget(
                            repo=repo,
                            sha=sha,
                            event="pull_request",
                            source=f"pr:{number}",
                            checkout_submodules=bool(
                                repo.raw.get("checkout_submodules")
                            ),
                            publish=False,
                            check_mode=scope.check_mode,
                            changed_paths=scope.changed_paths,
                            changed_files=tuple(changed_files or ()),
                            fast_path_reason=scope.fast_path_reason,
                        )
                    )
        if include_main:
            sha = _main_sha(repo)
            if sha:
                changed_paths = (
                    None
                    if repo.publish_profile == "template"
                    else _commit_changed_paths(repo, sha)
                )
                changed_file_statuses: dict[str, str] = {}
                if changed_paths is None:
                    components = []
                else:
                    components = publish_components_for_changed_paths(
                        repo, changed_paths
                    )
                    if not components:
                        components = _recent_main_publish_components(repo)
                scope = classify_required_check_scope(
                    repo,
                    changed_paths,
                    changed_file_statuses=changed_file_statuses,
                    publish=bool(components),
                )
                targets.append(
                    PollTarget(
                        repo=repo,
                        sha=sha,
                        event="push",
                        source="main",
                        checkout_submodules=bool(repo.raw.get("checkout_submodules")),
                        publish=bool(components),
                        publish_components=tuple(components),
                        check_mode=scope.check_mode,
                        changed_paths=scope.changed_paths,
                        changed_files=(),
                        fast_path_reason=scope.fast_path_reason,
                    )
                )
    return targets


def publish_required(repo: RepoConfig, *, sha: str, event: str) -> bool:
    return bool(publish_components_required(repo, sha=sha, event=event))


def publish_components_required(repo: RepoConfig, *, sha: str, event: str) -> list[str]:
    if event != "push" or repo.publish_profile == "template":
        return []
    changed_paths = _commit_changed_paths(repo, sha)
    if changed_paths is None:
        raise PublishPathResolutionError(
            f"{repo.name}: unable to resolve changed files for {sha}; "
            "publish skipped until the commit can be inspected or manually published"
        )
    return publish_components_for_changed_paths(repo, changed_paths)


def resolve_changed_files(
    repo: RepoConfig, *, sha: str, event: str, source: str
) -> list[dict[str, str]] | None:
    if event == "pull_request":
        if source.startswith("pr:"):
            return _pull_request_changed_files_at_head(
                repo, source.removeprefix("pr:"), sha
            )
        return None
    if event == "push":
        return _commit_changed_files(repo, sha)
    return None


def _commit_changed_files(repo: RepoConfig, sha: str) -> list[dict[str, str]] | None:
    result = _gh(
        [
            "api",
            f"repos/{repo.github_repo}/commits/{sha}",
            "--jq",
            ".files[] | {path: .filename, status: .status, previous_path: .previous_filename}",
        ]
    )
    if result.returncode != 0:
        return None
    files: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            files.extend(_changed_file_entries(payload))
    return files


def _commit_changed_paths(repo: RepoConfig, sha: str) -> list[str] | None:
    return _changed_file_paths(_commit_changed_files(repo, sha))


def _recent_main_publish_components(repo: RepoConfig, limit: int = 20) -> list[str]:
    commits = _main_commit_shas(repo, limit=limit)
    if not commits:
        return []
    seen_paths: list[str] = []
    for commit_sha in commits[1:]:
        if check_run_satisfied(repo, sha=commit_sha, event="push"):
            break
        changed_paths = _commit_changed_paths(repo, commit_sha)
        if changed_paths is None:
            return publish_component_names(repo)
        seen_paths.extend(changed_paths)
    return publish_components_for_changed_paths(repo, seen_paths)


def _main_commit_shas(repo: RepoConfig, *, limit: int = 20) -> list[str]:
    result = _gh(
        [
            "api",
            f"repos/{repo.github_repo}/commits",
            "-f",
            "sha=main",
            "-f",
            f"per_page={limit}",
            "--jq",
            ".[].sha",
        ]
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _pull_request_changed_files(
    repo: RepoConfig, number: str
) -> list[dict[str, str]] | None:
    result = _gh(
        [
            "api",
            "--paginate",
            f"repos/{repo.github_repo}/pulls/{number}/files",
            "--jq",
            ".[] | {path: .filename, status: .status, previous_path: .previous_filename}",
        ]
    )
    if result.returncode != 0:
        return None
    files: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            files.extend(_changed_file_entries(payload))
    return files


def _pull_request_changed_files_at_head(
    repo: RepoConfig, number: str, sha: str
) -> list[dict[str, str]] | None:
    if not number or not sha:
        return None
    if _pull_request_head_sha(repo, number) != sha:
        return None
    files = _pull_request_changed_files(repo, number)
    if files is None:
        return None
    if _pull_request_head_sha(repo, number) != sha:
        return None
    return files


def _pull_request_head_sha(repo: RepoConfig, number: str) -> str:
    result = _gh(
        ["api", f"repos/{repo.github_repo}/pulls/{number}", "--jq", ".head.sha"]
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _pull_request_changed_paths(repo: RepoConfig, number: str) -> list[str] | None:
    return _changed_file_paths(_pull_request_changed_files(repo, number))


def _changed_file_paths(
    changed_files: list[dict[str, str]] | None,
) -> list[str] | None:
    if changed_files is None:
        return None
    paths = [item["path"] for item in changed_files if item.get("path")]
    return paths


def _changed_file_entries(payload: dict[str, object]) -> list[dict[str, str]]:
    path = _json_text(payload.get("path"))
    status = _json_text(payload.get("status"))
    previous_path = _json_text(payload.get("previous_path"))
    files: list[dict[str, str]] = []
    if path:
        files.append({"path": path, "status": status})
    if previous_path and previous_path != path:
        files.append({"path": previous_path, "status": "renamed-from"})
    return files


def _json_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _changed_file_statuses(
    changed_files: list[dict[str, str]] | None,
) -> dict[str, str]:
    if not changed_files:
        return {}
    return {
        item["path"]: item.get("status", "")
        for item in changed_files
        if item.get("path")
    }


def _open_pull_requests(repo: RepoConfig) -> list[dict[str, object]]:
    result = _gh(
        [
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--state",
            "open",
            "--json",
            "number,headRefOid,headRepository,headRepositoryOwner,isCrossRepository",
        ]
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout or "[]")


def _same_repository_pull_request(
    repo: RepoConfig, pull_request: dict[str, object]
) -> bool:
    if pull_request.get("isCrossRepository") is True:
        return False
    if pull_request.get("isCrossRepository") is False:
        return True

    expected = repo.github_repo.casefold()
    head_repository = pull_request.get("headRepository")
    if isinstance(head_repository, dict):
        name_with_owner = _string_value(head_repository.get("nameWithOwner"))
        if name_with_owner:
            return name_with_owner.casefold() == expected

        name = _string_value(head_repository.get("name"))
        owner = _head_repository_owner(pull_request)
        if name and owner:
            return f"{owner}/{name}".casefold() == expected

    return False


def _head_repository_owner(pull_request: dict[str, object]) -> str:
    owner = pull_request.get("headRepositoryOwner")
    if isinstance(owner, dict):
        return _string_value(owner.get("login"))
    return _string_value(owner)


def _string_value(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _main_sha(repo: RepoConfig) -> str:
    result = _gh(["api", f"repos/{repo.github_repo}/commits/main", "--jq", ".sha"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    gh = shutil.which("gh")
    if gh is None:
        return subprocess.CompletedProcess(["gh", *args], 127, "", "gh CLI is required")
    return subprocess.run(
        [gh, *args],
        check=False,
        text=True,
        capture_output=True,
        env=github_cli_env(),
    )  # nosec B603


def github_cli_env() -> dict[str, str] | None:
    token = _github_cli_token()
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def _github_cli_token() -> str:
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "APP_TOKEN",
        "AIO_FLEET_CHECK_TOKEN",
        "AIO_FLEET_WORKFLOW_TOKEN",
    ):
        token = os.environ.get(key, "").strip()
        if token:
            return token
    return ""
