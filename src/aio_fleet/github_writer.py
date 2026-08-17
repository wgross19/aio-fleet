from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # nosec B404
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aio_fleet.github_app import read_urlopen_with_retry, resolve_token
from aio_fleet.manifest import RepoConfig

API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class BranchCommitResult:
    action: str
    branch: str
    sha: str
    method: str
    verified: bool
    verification: dict[str, Any] = field(default_factory=dict)
    committed_paths: list[str] = field(default_factory=list)


def commit_paths_to_branch(
    repo: RepoConfig,
    *,
    branch: str,
    paths: list[str],
    message: str,
    base: str = "main",
    token: str | None = None,
    require_verified: bool = True,
    mode: str | None = None,
) -> BranchCommitResult:
    commit_mode = (
        mode or os.environ.get("AIO_FLEET_UPSTREAM_COMMIT_MODE", "api")
    ).strip()
    if commit_mode == "git-signed":
        return _commit_with_git(
            repo,
            branch=branch,
            paths=paths,
            message=message,
            base=base,
            token=token,
            require_verified=require_verified,
            sign=True,
        )
    if commit_mode != "api":
        raise ValueError(f"unsupported upstream commit mode: {commit_mode}")
    return _commit_with_contents_api(
        repo,
        branch=branch,
        paths=paths,
        message=message,
        base=base,
        token=token,
        require_verified=require_verified,
    )


def commit_verification(
    repo: RepoConfig,
    *,
    sha: str,
    token: str | None = None,
) -> dict[str, Any]:
    owner, repo_name = repo.github_repo.split("/", 1)
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/commits/{sha}",
        token=_token(token),
        method="GET",
    )
    commit = response.get("commit", {})
    verification = commit.get("verification", {})
    return verification if isinstance(verification, dict) else {}


def _commit_with_contents_api(
    repo: RepoConfig,
    *,
    branch: str,
    paths: list[str],
    message: str,
    base: str,
    token: str | None,
    require_verified: bool,
) -> BranchCommitResult:
    safe_paths = [
        _safe_repo_path(repo.path, path, repo_name=repo.name)[0] for path in paths
    ]
    if len(safe_paths) > 1 or any(
        _is_gitlink_path(repo.path, path) for path in safe_paths
    ):
        return _commit_with_git_data_api(
            repo,
            branch=branch,
            paths=safe_paths,
            message=message,
            base=base,
            token=token,
            require_verified=require_verified,
        )
    validated_paths = [
        (path, _safe_repo_file(repo.path, path, repo_name=repo.name))
        for path in safe_paths
    ]

    token = _token(token)
    owner, repo_name = repo.github_repo.split("/", 1)
    branch_ref = f"heads/{branch}"
    base_sha = _ref_sha(owner, repo_name, f"heads/{base}", token=token)
    old_sha = _optional_ref_sha(owner, repo_name, branch_ref, token=token)
    created_branch = old_sha is None
    commit_shas: list[str] = []
    committed_paths: list[str] = []
    try:
        if old_sha:
            _update_ref(owner, repo_name, branch_ref, sha=base_sha, token=token)
        else:
            _create_ref(owner, repo_name, branch_ref, sha=base_sha, token=token)
        for relative_path, local_path in validated_paths:
            response = _put_contents(
                owner,
                repo_name,
                branch=branch,
                path=relative_path,
                content=local_path.read_bytes(),
                message=message,
                token=token,
            )
            commit = response.get("commit", {})
            sha = str(commit.get("sha") or "")
            if not sha:
                raise RuntimeError(f"{repo.name}: GitHub did not return a commit SHA")
            verification = _verification_from_response(repo, sha=sha, token=token)
            if require_verified and not verification.get("verified"):
                reason = verification.get("reason", "unknown")
                raise RuntimeError(
                    f"{repo.name}: GitHub API commit {sha} is not verified: {reason}"
                )
            commit_shas.append(sha)
            committed_paths.append(relative_path)
    except Exception:
        _restore_branch(
            owner,
            repo_name,
            branch_ref,
            old_sha=old_sha,
            created_branch=created_branch,
            token=token,
        )
        raise

    head = _ref_sha(owner, repo_name, branch_ref, token=token)
    verification = _verification_from_response(repo, sha=head, token=token)
    return BranchCommitResult(
        action="committed",
        branch=branch,
        sha=head,
        method="api",
        verified=bool(verification.get("verified")),
        verification=verification,
        committed_paths=committed_paths,
    )


def _commit_with_git_data_api(
    repo: RepoConfig,
    *,
    branch: str,
    paths: list[str],
    message: str,
    base: str,
    token: str | None,
    require_verified: bool,
) -> BranchCommitResult:
    safe_paths = [
        _safe_repo_path(repo.path, path, repo_name=repo.name)[0] for path in paths
    ]
    token = _token(token)
    owner, repo_name = repo.github_repo.split("/", 1)
    branch_ref = f"heads/{branch}"
    base_sha = _ref_sha(owner, repo_name, f"heads/{base}", token=token)
    old_sha = _optional_ref_sha(owner, repo_name, branch_ref, token=token)
    created_branch = old_sha is None
    try:
        base_commit = _git_commit(owner, repo_name, base_sha, token=token)
        base_tree = _tree_sha(base_commit, owner, repo_name, base_sha)
        entries = [
            _tree_entry_for_path(owner, repo_name, repo.path, path, token=token)
            for path in safe_paths
        ]
        tree_sha = _create_tree(
            owner,
            repo_name,
            base_tree=base_tree,
            entries=entries,
            token=token,
        )
        if tree_sha == base_tree:
            if old_sha:
                _update_ref(owner, repo_name, branch_ref, sha=base_sha, token=token)
            return BranchCommitResult(
                action="no-diff",
                branch=branch,
                sha=base_sha,
                method="api",
                verified=True,
                verification={"verified": True, "reason": "no-diff"},
                committed_paths=[],
            )
        commit_sha = _create_commit(
            owner,
            repo_name,
            message=message,
            tree=tree_sha,
            parents=[base_sha],
            token=token,
        )
        if old_sha:
            _update_ref(owner, repo_name, branch_ref, sha=commit_sha, token=token)
        else:
            _create_ref(owner, repo_name, branch_ref, sha=commit_sha, token=token)
        verification = _verification_from_response(repo, sha=commit_sha, token=token)
        if require_verified and not verification.get("verified"):
            reason = verification.get("reason", "unknown")
            raise RuntimeError(
                f"{repo.name}: GitHub API commit {commit_sha} is not verified: {reason}"
            )
        return BranchCommitResult(
            action="committed",
            branch=branch,
            sha=commit_sha,
            method="api",
            verified=bool(verification.get("verified")),
            verification=verification,
            committed_paths=safe_paths,
        )
    except Exception:
        _restore_branch(
            owner,
            repo_name,
            branch_ref,
            old_sha=old_sha,
            created_branch=created_branch,
            token=token,
        )
        raise


def _commit_with_git(
    repo: RepoConfig,
    *,
    branch: str,
    paths: list[str],
    message: str,
    base: str,
    token: str | None,
    require_verified: bool,
    sign: bool,
) -> BranchCommitResult:
    safe_paths = [
        _safe_repo_path(repo.path, path, repo_name=repo.name)[0] for path in paths
    ]
    _run_git(repo.path, ["fetch", "origin", base])
    _run_git(repo.path, ["checkout", "-B", branch, f"origin/{base}"])
    _run_git(repo.path, ["add", *safe_paths])
    diff = _run_git(repo.path, ["diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        sha = _run_git(repo.path, ["rev-parse", "HEAD"]).stdout.strip()
        verification = _verification_from_response(repo, sha=sha, token=token)
        return BranchCommitResult(
            action="no-diff",
            branch=branch,
            sha=sha,
            method="git-signed" if sign else "git",
            verified=bool(verification.get("verified")),
            verification=verification,
            committed_paths=[],
        )
    command = ["commit", "-m", message]
    if sign:
        command.insert(1, "-S")
    _run_git(repo.path, command)
    _run_git(repo.path, ["fetch", "origin", branch], check=False)
    _run_git(repo.path, ["push", "--force-with-lease", "-u", "origin", branch])
    sha = _run_git(repo.path, ["rev-parse", "HEAD"]).stdout.strip()
    verification = _verification_from_response(repo, sha=sha, token=token)
    if require_verified and not verification.get("verified"):
        reason = verification.get("reason", "unknown")
        raise RuntimeError(
            f"{repo.name}: pushed commit {sha} is not verified: {reason}"
        )
    return BranchCommitResult(
        action="committed",
        branch=branch,
        sha=sha,
        method="git-signed" if sign else "git",
        verified=bool(verification.get("verified")),
        verification=verification,
        committed_paths=safe_paths,
    )


def _put_contents(
    owner: str,
    repo_name: str,
    *,
    branch: str,
    path: str,
    content: bytes,
    message: str,
    token: str,
) -> dict[str, Any]:
    current = _optional_contents(
        owner, repo_name, branch=branch, path=path, token=token
    )
    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if current and current.get("sha"):
        payload["sha"] = current["sha"]
    return _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/contents/{_quote_path(path)}",
        token=token,
        method="PUT",
        payload=payload,
    )


def _optional_contents(
    owner: str,
    repo_name: str,
    *,
    branch: str,
    path: str,
    token: str,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"ref": branch})
    try:
        response = _github_request(
            f"https://api.github.com/repos/{owner}/{repo_name}/contents/{_quote_path(path)}?{query}",
            token=token,
            method="GET",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return response if isinstance(response, dict) else None


def _verification_from_response(
    repo: RepoConfig,
    *,
    sha: str,
    token: str | None,
) -> dict[str, Any]:
    verification = commit_verification(repo, sha=sha, token=token)
    return verification or {"verified": False, "reason": "missing-verification"}


def _git_commit(
    owner: str,
    repo_name: str,
    sha: str,
    *,
    token: str,
) -> dict[str, Any]:
    return _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/commits/{sha}",
        token=token,
        method="GET",
    )


def _tree_sha(
    commit: dict[str, Any], owner: str, repo_name: str, commit_sha: str
) -> str:
    tree = commit.get("tree", {})
    sha = str(tree.get("sha") or "") if isinstance(tree, dict) else ""
    if not sha:
        raise RuntimeError(
            f"{owner}/{repo_name}: unable to resolve tree for {commit_sha}"
        )
    return sha


def _create_blob(
    owner: str,
    repo_name: str,
    *,
    content: bytes,
    token: str,
) -> str:
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/blobs",
        token=token,
        method="POST",
        payload={
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        },
    )
    sha = str(response.get("sha") or "")
    if not sha:
        raise RuntimeError(f"{owner}/{repo_name}: GitHub did not return a blob SHA")
    return sha


def _create_tree(
    owner: str,
    repo_name: str,
    *,
    base_tree: str,
    entries: list[dict[str, str]],
    token: str,
) -> str:
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/trees",
        token=token,
        method="POST",
        payload={"base_tree": base_tree, "tree": entries},
    )
    sha = str(response.get("sha") or "")
    if not sha:
        raise RuntimeError(f"{owner}/{repo_name}: GitHub did not return a tree SHA")
    return sha


def _create_commit(
    owner: str,
    repo_name: str,
    *,
    message: str,
    tree: str,
    parents: list[str],
    token: str,
) -> str:
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/commits",
        token=token,
        method="POST",
        payload={"message": message, "tree": tree, "parents": parents},
    )
    sha = str(response.get("sha") or "")
    if not sha:
        raise RuntimeError(f"{owner}/{repo_name}: GitHub did not return a commit SHA")
    return sha


def _tree_entry_for_path(
    owner: str,
    repo_name: str,
    repo_path: Path,
    relative_path: str,
    *,
    token: str,
) -> dict[str, str]:
    safe_path, local_path = _safe_repo_path(
        repo_path,
        relative_path,
        repo_name=f"{owner}/{repo_name}",
    )
    relative_path = safe_path
    index_entry = _git_index_entry(repo_path, relative_path)
    if index_entry and index_entry[0] == "160000":
        gitlink_sha = index_entry[1]
        if local_path.exists():
            gitlink_sha = _run_git(local_path, ["rev-parse", "HEAD"]).stdout.strip()
        return {
            "path": relative_path,
            "mode": "160000",
            "type": "commit",
            "sha": gitlink_sha,
        }
    local_path = _safe_repo_file(
        repo_path,
        relative_path,
        repo_name=f"{owner}/{repo_name}",
    )
    if not local_path.is_file():
        raise RuntimeError(f"missing generated path: {relative_path}")
    blob_sha = _create_blob(
        owner,
        repo_name,
        content=local_path.read_bytes(),
        token=token,
    )
    mode = index_entry[0] if index_entry else "100644"
    if mode not in {"100644", "100755"}:
        mode = "100644"
    return {
        "path": relative_path,
        "mode": mode,
        "type": "blob",
        "sha": blob_sha,
    }


def _is_gitlink_path(repo_path: Path, relative_path: str) -> bool:
    entry = _git_index_entry(repo_path, relative_path)
    return bool(entry and entry[0] == "160000")


def _safe_repo_path(
    repo_path: Path,
    relative_path: str,
    *,
    repo_name: str,
) -> tuple[str, Path]:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RuntimeError(f"{repo_name}: invalid commit path: {relative_path}")
    local_path = repo_path / relative
    if local_path.is_symlink():
        raise RuntimeError(f"{repo_name}: invalid commit path: {relative_path}")
    resolved_repo = repo_path.resolve()
    if local_path.exists() and not local_path.resolve().is_relative_to(resolved_repo):
        raise RuntimeError(f"{repo_name}: invalid commit path: {relative_path}")
    return relative.as_posix(), local_path


def _safe_repo_file(repo_path: Path, relative_path: str, *, repo_name: str) -> Path:
    _, local_path = _safe_repo_path(repo_path, relative_path, repo_name=repo_name)
    if not local_path.is_file():
        raise RuntimeError(f"{repo_name}: missing generated path: {relative_path}")
    return local_path


def _git_index_entry(repo_path: Path, relative_path: str) -> tuple[str, str] | None:
    result = _run_git(
        repo_path,
        ["ls-files", "-s", "--", relative_path],
        check=False,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _ref_sha(owner: str, repo_name: str, ref: str, *, token: str) -> str:
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/ref/{_quote_ref(ref)}",
        token=token,
        method="GET",
    )
    obj = response.get("object", {})
    sha = str(obj.get("sha") or "")
    if not sha:
        raise RuntimeError(f"{owner}/{repo_name}: unable to resolve ref {ref}")
    return sha


def _optional_ref_sha(
    owner: str,
    repo_name: str,
    ref: str,
    *,
    token: str,
) -> str | None:
    try:
        return _ref_sha(owner, repo_name, ref, token=token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _create_ref(
    owner: str,
    repo_name: str,
    ref: str,
    *,
    sha: str,
    token: str,
) -> None:
    _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/refs",
        token=token,
        method="POST",
        payload={"ref": f"refs/{ref}", "sha": sha},
    )


def _update_ref(
    owner: str,
    repo_name: str,
    ref: str,
    *,
    sha: str,
    token: str,
) -> None:
    _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/refs/{_quote_ref(ref)}",
        token=token,
        method="PATCH",
        payload={"sha": sha, "force": True},
    )


def _delete_ref(owner: str, repo_name: str, ref: str, *, token: str) -> None:
    _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/refs/{_quote_ref(ref)}",
        token=token,
        method="DELETE",
    )


def _restore_branch(
    owner: str,
    repo_name: str,
    ref: str,
    *,
    old_sha: str | None,
    created_branch: bool,
    token: str,
) -> None:
    try:
        if created_branch:
            _delete_ref(owner, repo_name, ref, token=token)
        elif old_sha:
            _update_ref(owner, repo_name, ref, sha=old_sha, token=token)
    except Exception:
        return


def _github_request(
    url: str,
    *,
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # nosec B310
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    raw = read_urlopen_with_retry(request, timeout=30).decode("utf-8")
    return json.loads(raw or "{}")


def _run_git(
    cwd: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git CLI is required")
    result = subprocess.run(  # nosec B603
        [git, "-c", "core.fsmonitor=", *args],
        cwd=cwd,
        env=_safe_git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def _safe_git_env() -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
        if key in os.environ
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _quote_path(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _quote_ref(ref: str) -> str:
    return urllib.parse.quote(ref, safe="/")


def _token(token: str | None) -> str:
    resolved = token or resolve_token(
        fallback_envs=("AIO_FLEET_CHECK_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
    )
    if not resolved:
        raise RuntimeError(
            "GitHub App credentials or a GitHub token are required for verified commits"
        )
    return resolved
