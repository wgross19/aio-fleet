from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

from aio_fleet.control_plane import _step_environment
from aio_fleet.manifest import RepoConfig
from aio_fleet.trunk_overlay import copy_trunk_overlay

HOOK_NAMES = ("pre-commit", "pre-push")


def run_local_trunk_overlay(
    repo: RepoConfig, *, fix: bool = False, all_files: bool = True
) -> subprocess.CompletedProcess[str]:
    trunk = os.environ.get("TRUNK_PATH") or shutil.which("trunk")
    if trunk is None:
        return subprocess.CompletedProcess(
            ["trunk"], 127, "", "trunk CLI is not installed"
        )

    aio_root = Path(__file__).resolve().parents[2]
    central_trunk = aio_root / ".trunk"
    if not (central_trunk / "trunk.yaml").exists():
        return subprocess.CompletedProcess(
            ["trunk"], 127, "", f"central Trunk config not found: {central_trunk}"
        )

    original_hooks_path = _git_config_get_optional("core.hooksPath", cwd=repo.path)
    repo_trunk = repo.path / ".trunk"
    created_overlay = False
    backup_trunk: Path | None = None
    try:
        if repo_trunk.exists():
            backup_trunk = _temporary_trunk_backup_path(repo.path)
            repo_trunk.rename(backup_trunk)

        created_overlay = True
        copy_trunk_overlay(central_trunk, repo_trunk)

        command = [
            trunk,
            "check",
            "--show-existing",
            "--no-progress",
            "--color=false",
            "--ignore=.trunk/**",
            "--ci",
            "--fix" if fix else "--no-fix",
        ]
        if backup_trunk is not None:
            command.insert(6, f"--ignore={backup_trunk.name}/**")
        if all_files:
            command.insert(3, "--all")
        env = _step_environment(inherit_secrets=False)
        env.setdefault("FORCE_COLOR", "0")
        return subprocess.run(  # nosec B603
            command,
            cwd=repo.path,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )
    finally:
        _restore_git_config("core.hooksPath", original_hooks_path, cwd=repo.path)
        if created_overlay:
            shutil.rmtree(repo_trunk, ignore_errors=True)
        if backup_trunk is not None and backup_trunk.exists():
            if repo_trunk.exists():
                shutil.rmtree(repo_trunk, ignore_errors=True)
            backup_trunk.rename(repo_trunk)


def _temporary_trunk_backup_path(repo_path: Path) -> Path:
    for index in range(100):
        suffix = f".trunk.aio-fleet-backup-{os.getpid()}"
        if index:
            suffix = f"{suffix}-{index}"
        candidate = repo_path / suffix
        if not candidate.exists():
            return candidate
    raise RuntimeError("unable to allocate temporary Trunk backup path")


def install_local_hooks(
    repo: RepoConfig,
    *,
    aio_root: Path | None = None,
    python: str | None = None,
    target_kind: str = "repo",
) -> Path:
    repo_path = repo.path.resolve()
    aio_root = (aio_root or Path(__file__).resolve().parents[2]).resolve()
    python = python or sys.executable
    git_dir = _git_dir(repo_path)
    hooks_dir = git_dir / "aio-fleet-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    for hook_name in HOOK_NAMES:
        hook_path = hooks_dir / hook_name
        hook_path.write_text(
            _hook_script(
                hook_name,
                repo_name=repo.name,
                aio_root=aio_root,
                python=python,
                target_kind=target_kind,
            )
        )
        hook_path.chmod(0o755)

    _run_git(["config", "core.hooksPath", str(hooks_dir)], cwd=repo_path)
    return hooks_dir


def _git_dir(repo_path: Path) -> Path:
    result = _run_git(["rev-parse", "--git-dir"], cwd=repo_path)
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    return git_dir.resolve()


def _run_git(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # nosec B603 B607
        ["git", *command],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(command)} failed: {detail}")
    return result


def _git_config_get_optional(key: str, *, cwd: Path) -> str | None:
    result = subprocess.run(  # nosec B603 B607
        ["git", "config", "--get", key],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _restore_git_config(key: str, value: str | None, *, cwd: Path) -> None:
    if value is None:
        subprocess.run(  # nosec B603 B607
            ["git", "config", "--unset", key],
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
        )
        return
    _run_git(["config", key, value], cwd=cwd)


def _hook_script(
    hook_name: str,
    *,
    repo_name: str,
    aio_root: Path,
    python: str,
    target_kind: str,
) -> str:
    if target_kind == "catalog":
        trunk_repo_args = '--repo-path "${repo_root}"'
        validation = 'run_fleet validate-catalog --catalog-path "${repo_root}"'
    else:
        trunk_repo_args = '--repo "${repo_name}" --repo-path "${repo_root}"'
        validation = (
            'run_fleet validate-repo --repo "${repo_name}" --repo-path "${repo_root}"'
        )

    if hook_name == "pre-commit":
        mutation_guard = f"""
before_diff="$(git -C "${{repo_root}}" diff --binary -- . | shasum | awk '{{print $1}}')"
run_fleet trunk run {trunk_repo_args} --local --changed --fix
after_diff="$(git -C "${{repo_root}}" diff --binary -- . | shasum | awk '{{print $1}}')"
if [[ "${{before_diff}}" != "${{after_diff}}" ]]; then
  echo "aio-fleet hook: Trunk fixed files; review and stage the changes, then commit again." >&2
  exit 1
fi
"""
    else:
        mutation_guard = (
            f"run_fleet trunk run {trunk_repo_args} --local --changed --no-fix\n"
        )

    return f"""#!/usr/bin/env bash
set -euo pipefail

repo_name={sh_quote(repo_name)}
repo_root="$(git rev-parse --show-toplevel)"
aio_root="${{AIO_FLEET_ROOT:-}}"
python_bin="${{AIO_FLEET_PYTHON:-}}"

if [[ -z "${{aio_root}}" ]]; then
  aio_root={sh_quote(str(aio_root))}
fi

if [[ -z "${{python_bin}}" ]]; then
  python_bin={sh_quote(python)}
fi

if [[ ! -x "${{python_bin}}" ]]; then
  python_bin="$(command -v python3 || command -v python)"
fi

run_fleet() {{
  (
    cd "${{aio_root}}"
    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
    "${{python_bin}}" -m aio_fleet "$@"
  )
}}

{mutation_guard}{validation}
"""


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
