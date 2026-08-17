"""Published-image smoke tests.

Build-time integration tests cover the commit on a PR, but nothing re-verifies
that the *published* `:latest` image still pulls and boots — and they only run
on linux/amd64 even though the fleet publishes multi-arch manifests, so a broken
linux/arm64 layer ships untested. This module pulls each published image on a
target architecture, boots it, and waits for the container's Docker HEALTHCHECK
to report healthy (falling back to "stayed running" for images without a
HEALTHCHECK). It is read-only against the registry and needs no publish tokens,
so it runs for free on public-repo GitHub Actions (including the free arm64
runners).
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Any

from aio_fleet.manifest import FleetManifest, RepoConfig, load_manifest

# Architectures the fleet can publish, as they appear in `publish_platforms`.
_ARCHES = ("amd64", "arm64")


def _run(cmd: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        cmd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def smoke_targets(manifest: FleetManifest, *, arch: str) -> list[dict[str, Any]]:
    """Resolve the (repo, image, tag) targets to smoke for an architecture."""

    targets: list[dict[str, Any]] = []
    for repo in manifest.repos.values():
        if repo.publish_profile == "template":
            continue
        config = _smoke_config(repo)
        if not config.get("enabled", True):
            continue
        platforms = str(repo.get("publish_platforms", "linux/amd64,linux/arm64"))
        if f"linux/{arch}" not in platforms:
            continue
        targets.extend(
            _targets_for_image(
                repo=repo.name,
                image=str(repo.image_name),
                arch=arch,
                config=config,
                default_tags=["latest"],
            )
        )
        targets.extend(_component_smoke_targets(repo, base_config=config, arch=arch))
    return targets


def _smoke_config(repo: RepoConfig) -> dict[str, Any]:
    raw = repo.get("smoke_test", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _component_smoke_targets(
    repo: RepoConfig, *, base_config: dict[str, Any], arch: str
) -> list[dict[str, Any]]:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return []

    targets: list[dict[str, Any]] = []
    for component_config in components.values():
        if not isinstance(component_config, dict):
            continue
        raw_config = component_config.get("smoke_test")
        if not isinstance(raw_config, dict) or raw_config.get("enabled") is not True:
            continue
        image = str(component_config.get("image_name", "")).strip()
        if not image:
            continue
        platforms = str(
            component_config.get(
                "publish_platforms",
                repo.get("publish_platforms", "linux/amd64,linux/arm64"),
            )
        )
        if f"linux/{arch}" not in platforms:
            continue
        config = _merge_smoke_config(base_config, raw_config)
        default_tags = [
            str(tag) for tag in component_config.get("floating_tags", []) if str(tag)
        ] or ["latest"]
        targets.extend(
            _targets_for_image(
                repo=repo.name,
                image=image,
                arch=arch,
                config=config,
                default_tags=default_tags,
            )
        )
    return targets


def _merge_smoke_config(
    base_config: dict[str, Any], override_config: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(base_config)
    merged.update(override_config)
    merged["env"] = {
        **dict(base_config.get("env", {})),
        **dict(override_config.get("env", {})),
    }
    return merged


def _targets_for_image(
    *,
    repo: str,
    image: str,
    arch: str,
    config: dict[str, Any],
    default_tags: list[str],
) -> list[dict[str, Any]]:
    tags = config.get("tags")
    smoke_tags = (
        [str(tag) for tag in tags] if isinstance(tags, list) and tags else default_tags
    )
    return [
        {
            "repo": repo,
            "image": f"{image}:{tag}",
            "tag": tag,
            "arch": arch,
            "env": dict(config.get("env", {})),
            "start_period": int(config.get("start_period_s", 180)),
            "timeout": int(config.get("timeout_s", 300)),
        }
        for tag in smoke_tags
    ]


def smoke_one(target: dict[str, Any]) -> dict[str, Any]:
    """Pull, boot, and health-check a single published image target."""

    image = str(target["image"])
    platform = f"linux/{target['arch']}"
    result: dict[str, Any] = {
        "repo": target["repo"],
        "image": image,
        "arch": target["arch"],
        "status": "fail",
        "detail": "",
    }

    pull = _run(["docker", "pull", "--platform", platform, image])
    if pull.returncode != 0:
        result["detail"] = f"pull failed: {_tail(pull.stderr or pull.stdout)}"
        return result

    run_cmd = ["docker", "run", "-d", "--platform", platform, "--rm"]
    for key, value in target.get("env", {}).items():
        run_cmd += ["-e", f"{key}={value}"]
    run_cmd.append(image)
    started = _run(run_cmd)
    if started.returncode != 0:
        result["detail"] = f"run failed: {_tail(started.stderr or started.stdout)}"
        return result
    container = started.stdout.strip()

    try:
        status, detail = _await_health(
            container,
            start_period=int(target["start_period"]),
            timeout=int(target["timeout"]),
        )
        result["status"] = status
        result["detail"] = detail
    finally:
        _run(["docker", "rm", "-f", container], timeout=60)
    return result


def _await_health(
    container: str, *, start_period: int, timeout: int
) -> tuple[str, str]:
    deadline = start_period + timeout
    elapsed = 0
    interval = 5
    has_healthcheck = False
    while elapsed <= deadline:
        inspect = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}"
                "no-healthcheck:{{.State.Status}}{{end}}",
                container,
            ],
            timeout=30,
        )
        state = inspect.stdout.strip()
        if inspect.returncode != 0:
            return "fail", f"container exited early: {_tail(inspect.stderr)}"
        if state == "healthy":
            return "pass", "healthcheck reported healthy"
        if state.startswith("no-healthcheck:"):
            # No HEALTHCHECK in the image: require it to stay running past the
            # start period instead.
            running = state.split(":", 1)[1]
            if running != "running":
                return "fail", f"container not running: {running}"
            if elapsed >= start_period:
                return "pass", "no healthcheck; stayed running through start period"
        elif state == "unhealthy":
            return "fail", "healthcheck reported unhealthy"
        else:
            has_healthcheck = True
        time.sleep(interval)
        elapsed += interval
    suffix = "healthcheck never healthy" if has_healthcheck else "did not stabilize"
    return "fail", f"timed out after {deadline}s: {suffix}"


def _tail(text: str, *, limit: int = 240) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[-limit:]


def smoke_published_images(
    *,
    manifest_path: Path,
    arch: str,
    output_path: Path | None = None,
    github_output: Path | None = None,
) -> dict[str, Any]:
    """Smoke every published image for the architecture and build a report."""

    if arch not in _ARCHES:
        raise ValueError(f"unsupported smoke architecture: {arch}")
    manifest = load_manifest(manifest_path)
    targets = smoke_targets(manifest, arch=arch)
    results = [smoke_one(target) for target in targets]
    failures = [item for item in results if item["status"] != "pass"]
    report = {
        "arch": arch,
        "total": len(results),
        "passed": len(results) - len(failures),
        "failed": len(failures),
        "status": 0 if not failures else 1,
        "results": results,
    }
    if output_path is not None:
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"status={report['status']}\n")
            handle.write(f"failed={report['failed']}\n")
    return report
