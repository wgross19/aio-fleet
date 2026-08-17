from __future__ import annotations

import os
import shlex
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aio_fleet.manifest import RepoConfig
from aio_fleet.registry import compute_registry_tags
from aio_fleet.trunk_overlay import copy_trunk_overlay

_SECRET_ENV_EXACT = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "AIO_FLEET_ALERT_WEBHOOK_URL",
    "AIO_FLEET_APP_ID",
    "AIO_FLEET_APP_INSTALLATION_ID",
    "AIO_FLEET_APP_PRIVATE_KEY",
    "AIO_FLEET_CHECK_TOKEN",
    "AIO_FLEET_GHCR_TOKEN",
    "AIO_FLEET_KUMA_PUSH_URL",
    "AIO_FLEET_RELEASE_TOKEN",
    "APP_TOKEN",
    "DOCKERHUB_PASSWORD",
    "DOCKERHUB_TOKEN",
    "DOCKERHUB_USERNAME",
    "GH_TOKEN",
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "SSH_AGENT_PID",
    "SSH_AUTH_SOCK",
}
_SECRET_ENV_MARKERS = (
    "AUTHORIZATION",
    "COOKIE",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "WEBHOOK",
)


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str] | None = None
    stream_output: bool = False
    timeout_seconds: int | None = None
    inherit_secrets: bool = True


def central_check_steps(
    repo: RepoConfig,
    *,
    event: str,
    manifest_path: Path | None = None,
    publish: bool = False,
    publish_component_names: Sequence[str] | None = None,
    include_trunk: bool = True,
    include_integration: bool = True,
    include_github_prereleases: bool = True,
    include_app_checks: bool = True,
    include_publish_steps: bool = True,
) -> list[Step]:
    manifest_args = ["--manifest", str(manifest_path)] if manifest_path else []
    trusted_cwd = _trusted_aio_root()
    registry_publish_enabled = publish and repo.publish_profile != "template"
    selected_publish_components = (
        list(publish_component_names)
        if publish_component_names
        else publish_components(repo)
    )
    steps: list[Step] = []
    if include_app_checks:
        steps.extend(
            [
                Step(
                    "validate-repo",
                    [
                        sys.executable,
                        "-m",
                        "aio_fleet.cli",
                        *manifest_args,
                        "validate-repo",
                        "--repo",
                        repo.name,
                        "--repo-path",
                        str(repo.path),
                    ],
                    trusted_cwd,
                    inherit_secrets=False,
                ),
                Step(
                    "verify-caller",
                    [
                        sys.executable,
                        "-m",
                        "aio_fleet.cli",
                        *manifest_args,
                        "verify-caller",
                        "--repo",
                        repo.name,
                        "--repo-path",
                        str(repo.path),
                    ],
                    trusted_cwd,
                    inherit_secrets=False,
                ),
            ]
        )
    install = _install_test_dependencies_step(repo.path)
    if registry_publish_enabled and include_publish_steps:
        steps.append(_registry_publish_preflight_step(manifest_args))
    if include_app_checks and install is not None:
        steps.append(Step(**{**install.__dict__, "inherit_secrets": False}))
    generator = str(repo.get("generator_check_command", "") or "").strip()
    if include_app_checks and generator:
        steps.append(
            Step(
                "generator-check",
                shlex.split(generator),
                repo.path,
                inherit_secrets=False,
            )
        )
    unit_args = str(repo.get("unit_pytest_args", "") or "").strip()
    if include_app_checks and unit_args:
        steps.append(
            Step(
                "unit-tests",
                [_repo_python(repo.path), "-m", "pytest", *shlex.split(unit_args)],
                repo.path,
                inherit_secrets=False,
            )
        )
    integration_args = str(repo.get("integration_pytest_args", "") or "").strip()
    prebuilt_integration_image = False
    if (
        include_app_checks
        and include_integration
        and event in {"pull_request", "push", "release", "workflow_dispatch"}
        and integration_args
    ):
        if registry_publish_enabled:
            for build_step in _pytest_image_build_steps(
                repo, selected_publish_components
            ):
                steps.append(build_step)
            prebuilt_integration_image = True
        integration_env = _prebuilt_pytest_environment(
            repo, selected_publish_components
        )
        steps.append(
            Step(
                "integration-tests",
                [
                    _repo_python(repo.path),
                    "-m",
                    "pytest",
                    *shlex.split(integration_args),
                ],
                repo.path,
                env=integration_env if prebuilt_integration_image else None,
                timeout_seconds=_repo_timeout_seconds(
                    repo, "integration_timeout_seconds", default=1800
                ),
                inherit_secrets=False,
            )
        )
    if include_app_checks and include_trunk:
        steps.append(
            Step(
                "trunk",
                [
                    sys.executable,
                    "-m",
                    "aio_fleet.cli",
                    *manifest_args,
                    "trunk",
                    "run",
                    "--repo",
                    repo.name,
                    "--repo-path",
                    str(repo.path),
                    "--no-fix",
                ],
                trusted_cwd,
                inherit_secrets=False,
            )
        )
    if registry_publish_enabled and include_publish_steps:
        components = selected_publish_components
        for component in components:
            step_name = (
                "registry-publish"
                if components == ["aio"]
                else f"registry-publish-{component}"
            )
            steps.append(
                Step(
                    step_name,
                    [
                        sys.executable,
                        "-m",
                        "aio_fleet.cli",
                        *manifest_args,
                        "registry",
                        "publish",
                        "--repo",
                        repo.name,
                        "--repo-path",
                        str(repo.path),
                        "--component",
                        component,
                    ],
                    trusted_cwd,
                    stream_output=True,
                    timeout_seconds=_repo_timeout_seconds(
                        repo, "registry_publish_timeout_seconds", default=3600
                    ),
                )
            )
            if include_github_prereleases:
                release_step = _github_release_publish_step(
                    repo, component, manifest_args=manifest_args
                )
                if release_step is not None:
                    steps.append(release_step)
    return steps


def _trusted_aio_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _registry_publish_preflight_step(manifest_args: list[str]) -> Step:
    return Step(
        "registry-publish-preflight",
        [
            sys.executable,
            "-m",
            "aio_fleet.cli",
            *manifest_args,
            "registry",
            "preflight",
            "--mode",
            "publish",
            "--format",
            "json",
        ],
        _trusted_aio_root(),
    )


def run_steps(steps: list[Step], *, dry_run: bool = False) -> list[str]:
    failures: list[str] = []
    for step in steps:
        if dry_run:
            env_prefix = ""
            if step.env:
                env_prefix = " ".join(
                    f"{key}={shlex.quote(value)}"
                    for key, value in sorted(step.env.items())
                )
                env_prefix += " "
            print(
                f"{step.name}: {env_prefix}"
                f"{' '.join(shlex.quote(part) for part in step.command)}"
            )
            continue
        env = _step_environment(step.env, inherit_secrets=step.inherit_secrets)
        if step.stream_output:
            try:
                result = subprocess.run(  # nosec B603
                    step.command,
                    cwd=step.cwd,
                    check=False,
                    text=True,
                    env=env,
                    timeout=step.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                timeout = step.timeout_seconds or 0
                failures.append(f"{step.name}: timed out after {timeout}s")
                break
            if result.returncode != 0:
                failures.append(f"{step.name}: exit {result.returncode}")
                break
            continue
        try:
            result = subprocess.run(  # nosec B603
                step.command,
                cwd=step.cwd,
                check=False,
                text=True,
                capture_output=True,
                env=env,
                timeout=step.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                print(_timeout_output_text(exc.stdout), end="")
            if exc.stderr:
                print(_timeout_output_text(exc.stderr), file=sys.stderr, end="")
            timeout = step.timeout_seconds or 0
            failures.append(f"{step.name}: timed out after {timeout}s")
            break
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode != 0:
            detail = _failure_detail(result.stdout, result.stderr)
            suffix = f": {detail}" if detail else ""
            failures.append(f"{step.name}: exit {result.returncode}{suffix}")
            break
    return failures


def _timeout_output_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _failure_detail(stdout: str, stderr: str) -> str:
    priority_markers = (
        "error",
        "failed",
        "failure",
        "incorrect",
        "not formatted",
        "would reformat",
        "refusing",
        "blocked",
    )
    for output in (stderr, stdout):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        priority = [
            line
            for line in lines
            if any(marker in line.lower() for marker in priority_markers)
        ]
        if priority:
            return priority[-1]
        if lines:
            return lines[-1]
    return ""


def _step_environment(
    extra_env: dict[str, str] | None = None, *, inherit_secrets: bool = True
) -> dict[str, str]:
    env = (
        dict(os.environ)
        if inherit_secrets
        else {
            key: value
            for key, value in os.environ.items()
            if not _secret_environment_key(key)
        }
    )
    if extra_env:
        unsafe_keys = (
            sorted(key for key in extra_env if _secret_environment_key(key))
            if not inherit_secrets
            else []
        )
        if unsafe_keys:
            raise ValueError(
                "refusing to pass secret-like environment keys to repo step: "
                + ", ".join(unsafe_keys)
            )
        env.update(extra_env)
    return env


def _secret_environment_key(key: str) -> bool:
    upper = key.upper()
    return upper in _SECRET_ENV_EXACT or any(
        marker in upper for marker in _SECRET_ENV_MARKERS
    )


def _pytest_image_build_steps(
    repo: RepoConfig, components: Sequence[str]
) -> list[Step]:
    steps: list[Step] = []
    seen: set[str] = set()
    for component in components:
        step = _pytest_image_build_step(repo, component)
        if step is None:
            continue
        key = " ".join(step.command)
        if key in seen:
            continue
        seen.add(key)
        steps.append(step)
    return steps


def _pytest_image_build_step(repo: RepoConfig, component: str = "aio") -> Step | None:
    component_config = _component_config(repo, component)
    image_tag = str(
        component_config.get("pytest_image_tag", repo.get("pytest_image_tag", "")) or ""
    ).strip()
    if not image_tag:
        return None
    platform = str(
        component_config.get(
            "pytest_image_platform", repo.get("pytest_image_platform", "linux/amd64")
        )
        or "linux/amd64"
    )
    dockerfile = str(
        component_config.get(
            "pytest_dockerfile",
            component_config.get(
                "dockerfile", repo.get("pytest_dockerfile", "Dockerfile")
            ),
        )
        or "Dockerfile"
    )
    context = str(
        component_config.get(
            "pytest_build_context", repo.get("pytest_build_context", ".")
        )
        or "."
    )
    command = [
        "docker",
        "build",
        "--progress=plain",
        "--platform",
        platform,
        "-t",
        image_tag,
    ]
    if dockerfile != "Dockerfile":
        command.extend(["-f", dockerfile])
    command.append(context)
    return Step(
        (
            "build-pytest-image"
            if component == "aio"
            else f"build-pytest-image-{component}"
        ),
        command,
        repo.path,
        stream_output=True,
        timeout_seconds=_repo_timeout_seconds(
            repo, "pytest_image_build_timeout_seconds", default=1800
        ),
        inherit_secrets=False,
    )


def _prebuilt_pytest_environment(
    repo: RepoConfig, components: Sequence[str]
) -> dict[str, str]:
    env: dict[str, str] = {}
    for component in components:
        component_config = _component_config(repo, component)
        image_tag = str(
            component_config.get("pytest_image_tag", repo.get("pytest_image_tag", ""))
            or ""
        ).strip()
        if not image_tag:
            continue
        env_name = str(component_config.get("pytest_prebuilt_env", "") or "").strip()
        if not env_name and component == "aio":
            env_name = "AIO_PYTEST_USE_PREBUILT_IMAGE"
        if env_name:
            env[env_name] = "true"
    return env


def _repo_timeout_seconds(repo: RepoConfig, key: str, *, default: int) -> int:
    value = repo.get(key, default)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return max(timeout, 1)


def publish_components(repo: RepoConfig) -> list[str]:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return ["aio"]
    names = [
        name
        for name, config in components.items()
        if name == "aio" or (isinstance(config, dict) and config.get("image_name"))
    ]
    return names or ["aio"]


def registry_publish_command(
    repo: RepoConfig, *, sha: str, component: str = "aio"
) -> list[str]:
    tags = compute_registry_tags(repo, sha=sha, component=component)
    component_config = _component_config(repo, component)
    cache_scope = component_config.get(
        "docker_cache_scope", repo.get("docker_cache_scope")
    )
    platforms = component_config.get(
        "publish_platforms", repo.get("publish_platforms", "linux/amd64,linux/arm64")
    )
    command = [
        "docker",
        "buildx",
        "build",
        "--progress=plain",
        "--push",
        "--platform",
        str(platforms),
        "--cache-from",
        f"type=gha,scope={cache_scope}",
        "--cache-to",
        f"type=gha,mode=max,scope={cache_scope}",
        "--attest=type=provenance,mode=max",
        "--attest=type=sbom",
    ]
    dockerfile = component_config.get("dockerfile")
    if dockerfile:
        command.extend(["--file", str(dockerfile)])
    for tag in tags.all_tags:
        command.extend(["--tag", tag])
    for annotation in _component_oci_annotations(repo, component):
        command.extend(["--annotation", annotation])
    command.append(str(component_config.get("context", ".")))
    return command


def _component_config(repo: RepoConfig, component: str) -> dict[str, object]:
    components = repo.raw.get("components")
    if isinstance(components, dict):
        config = components.get(component)
        if isinstance(config, dict):
            return config
    return {}


def _component_oci_annotations(repo: RepoConfig, component: str) -> list[str]:
    config = _component_config(repo, component)
    source = str(
        config.get("oci_source", "") or f"https://github.com/{repo.github_repo}"
    ).strip()
    description = str(
        config.get("oci_description", "")
        or config.get("image_description", "")
        or repo.get("image_description", "")
        or f"{repo.name} container image"
    ).strip()
    annotations = [
        f"index:org.opencontainers.image.source={source}",
        f"index:org.opencontainers.image.description={description}",
    ]
    return [annotation for annotation in annotations if annotation.rsplit("=", 1)[-1]]


def _github_release_publish_step(
    repo: RepoConfig, component: str, *, manifest_args: list[str]
) -> Step | None:
    config = _component_config(repo, component)
    if str(config.get("release_history", "")).strip() != "github_prerelease":
        return None
    return Step(
        f"github-prerelease-{component}",
        [
            sys.executable,
            "-m",
            "aio_fleet.cli",
            *manifest_args,
            "release",
            "publish",
            "--repo",
            repo.name,
            "--repo-path",
            str(repo.path),
            "--component",
            component,
        ],
        _trusted_aio_root(),
        timeout_seconds=_repo_timeout_seconds(
            repo, "github_release_publish_timeout_seconds", default=300
        ),
    )


def run_central_trunk(
    repo: RepoConfig, *, fix: bool = False
) -> subprocess.CompletedProcess[str]:
    trunk = os.environ.get("TRUNK_PATH") or shutil.which("trunk")
    if trunk is None:
        return subprocess.CompletedProcess(
            ["trunk"], 127, "", "trunk CLI is not installed"
        )
    git = shutil.which("git")
    if git is None:
        return subprocess.CompletedProcess(["git"], 127, "", "git CLI is not installed")
    aio_root = Path(__file__).resolve().parents[2]
    central_trunk = aio_root / ".trunk"
    tmp_root = Path(os.environ.get("AIO_FLEET_TMPDIR") or tempfile.gettempdir())
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{repo.name}-trunk-", dir=tmp_root) as tmp:
        scratch = Path(tmp) / repo.name
        env = _step_environment(inherit_secrets=False)
        env.setdefault("FORCE_COLOR", "0")
        subprocess.run(
            [git, "clone", "--quiet", str(repo.path), str(scratch)],
            check=True,
            env=env,
        )  # nosec B603
        scratch_trunk = scratch / ".trunk"
        if scratch_trunk.exists():
            shutil.rmtree(scratch_trunk)
        copy_trunk_overlay(central_trunk, scratch_trunk)
        command = [
            trunk,
            "check",
            "--show-existing",
            "--all",
            "--no-progress",
            "--color=false",
            "--ignore=.trunk/**",
            "--fix" if fix else "--no-fix",
        ]
        return subprocess.run(  # nosec B603
            command, cwd=scratch, check=False, text=True, capture_output=True, env=env
        )


def _repo_python(repo_path: Path) -> str:
    # App checkouts are untrusted in central checks; never execute a repo-local
    # virtualenv shim before the policy, test, and publish gates finish.
    del repo_path
    return sys.executable


def _install_test_dependencies_step(repo_path: Path) -> Step | None:
    if (repo_path / "tests").exists():
        aio_root = _trusted_aio_root()
        return Step(
            "install-test-deps",
            [
                _repo_python(repo_path),
                "-m",
                "pip",
                "install",
                "-e",
                f"{aio_root}[app-tests]",
            ],
            repo_path,
        )
    return None
