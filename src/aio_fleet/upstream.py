from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from aio_fleet.changelog import component_config
from aio_fleet.checks import check_run_payload, upsert_check_run
from aio_fleet.github_writer import commit_paths_to_branch
from aio_fleet.manifest import RepoConfig
from aio_fleet.public_text import assert_public_text
from aio_fleet.safety import assess_expected_update, render_safety_summary

SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)
PRERELEASE_SUFFIXES = {
    "alpha",
    "beta",
    "canary",
    "dev",
    "nightly",
    "pre",
    "preview",
    "rc",
    "snapshot",
}
STABLE_MAINTENANCE_SUFFIXES = {"hotfix"}


class RegistryDigestNotFoundError(ValueError):
    """Raised when no manifest digest exists for an otherwise valid image tag."""


@dataclass(frozen=True)
class GitHubReleaseCandidate:
    tag: str
    version: str


@dataclass(frozen=True)
class UpstreamMonitorResult:
    repo: str
    component: str
    name: str
    strategy: str
    source: str
    current_version: str
    latest_version: str
    current_digest: str
    latest_digest: str
    version_update: bool
    digest_update: bool
    dockerfile: Path
    version_key: str
    digest_key: str
    release_notes_url: str
    commit_key: str = ""
    current_commit: str = ""
    latest_commit: str = ""
    commit_update: bool = False
    submodule_path: str = ""
    submodule_ref: str = ""
    skipped_versions: tuple[dict[str, str], ...] = ()
    blocked_reason: str = ""
    next_action: str = ""

    @property
    def updates_available(self) -> bool:
        return self.version_update or self.digest_update or self.commit_update

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)


def monitor_repo(
    repo: RepoConfig,
    *,
    write: bool = False,
) -> list[UpstreamMonitorResult]:
    configs = monitor_configs(repo)
    results = [evaluate_monitor(repo, config) for config in configs]
    results = _align_shared_release_digest_groups(configs, results)
    results = _with_submodule_ref_blockers(repo, configs, results)
    if write:
        _write_monitor_results(repo, configs, results)
    return results


def _write_monitor_results(
    repo: RepoConfig,
    configs: list[dict[str, Any]],
    results: list[UpstreamMonitorResult],
) -> None:
    for config, result in zip(configs, results, strict=True):
        if result.updates_available and result.strategy == "pr" and not result.blocked:
            write_arg(result.dockerfile, result.version_key, result.latest_version)
            if result.digest_key and result.latest_digest:
                write_arg(result.dockerfile, result.digest_key, result.latest_digest)
            if result.commit_key and result.latest_commit:
                write_arg(result.dockerfile, result.commit_key, result.latest_commit)
            _reset_registry_revision(repo, result)
            _update_release_history_changelog(repo, result)
            if result.version_update:
                update_submodule(repo, config, result)


def _with_submodule_ref_blockers(
    repo: RepoConfig,
    configs: list[dict[str, Any]],
    results: list[UpstreamMonitorResult],
) -> list[UpstreamMonitorResult]:
    checked = list(results)
    for index, (config, result) in enumerate(zip(configs, results, strict=True)):
        reason = _missing_submodule_ref_blocker(repo, config, result)
        if reason:
            checked[index] = replace(
                result,
                blocked_reason=reason,
                next_action=_missing_submodule_next_action(result),
            )
    return checked


def _missing_submodule_ref_blocker(
    repo: RepoConfig,
    config: dict[str, Any],
    result: UpstreamMonitorResult,
) -> str:
    if (
        not result.updates_available
        or result.strategy != "pr"
        or not result.version_update
        or not result.submodule_path
        or not result.submodule_ref
    ):
        return ""
    remote = str(config.get("submodule_remote", "")).strip()
    if not remote:
        return ""
    submodule_dir = repo.path / result.submodule_path
    if not submodule_dir.exists():
        return ""
    git = required_executable("git")
    probe = subprocess.run(  # nosec B603
        [git, "rev-parse", "--is-inside-work-tree"],
        cwd=submodule_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return ""
    ref_check = subprocess.run(  # nosec B603
        [git, "ls-remote", "--exit-code", remote, result.submodule_ref],
        cwd=submodule_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if ref_check.returncode == 2:
        return (
            f"missing configured submodule ref {result.submodule_ref} "
            f"on remote {remote}"
        )
    return ""


def _missing_submodule_next_action(result: UpstreamMonitorResult) -> str:
    return (
        f"create and push {result.submodule_ref} for {result.component} "
        f"before opening the {result.repo} upstream PR"
    )


def _align_shared_release_digest_groups(
    configs: list[dict[str, Any]],
    results: list[UpstreamMonitorResult],
) -> list[UpstreamMonitorResult]:
    grouped: dict[tuple[Path, str, str, bool, str], list[int]] = {}
    for index, (config, result) in enumerate(zip(configs, results, strict=True)):
        if (
            config.get("source") != "github-releases"
            or not result.digest_key
            or not str(config.get("image", "")).strip()
            or not str(config.get("digest_source", "")).strip()
            or not result.version_key
        ):
            continue
        key = (
            result.dockerfile.resolve(),
            result.version_key,
            str(config.get("repo", "")),
            bool(config.get("stable_only", True)),
            str(config.get("prerelease_channel", "")).strip(),
            str(config.get("version_strip_prefix", "")),
        )
        grouped.setdefault(key, []).append(index)

    aligned = list(results)
    for (
        _dockerfile,
        _version_key,
        upstream_repo,
        stable_only,
        prerelease_channel,
        strip_prefix,
    ), indexes in grouped.items():
        if len(indexes) < 2:
            continue
        candidates, skipped = github_release_candidates_result(
            upstream_repo,
            stable_only=stable_only,
            prerelease_channel=prerelease_channel,
            strip_prefix=strip_prefix,
        )
        current_version = aligned[indexes[0]].current_version
        current_key = (
            version_sort_key(current_version)
            if SEMVER_RE.match(current_version)
            else None
        )
        missing: list[dict[str, str]] = []
        selected_version = current_version
        selected_digests = {index: aligned[index].current_digest for index in indexes}
        selected_tag = candidates[0].tag

        for candidate in candidates:
            selected_tag = candidate.tag
            if (
                current_key is not None
                and version_sort_key(candidate.version) <= current_key
            ):
                if candidate.version != current_version:
                    missing.append(
                        {
                            "version": candidate.version,
                            "reason": "not-newer-than-current",
                        }
                    )
                break
            candidate_digests: dict[int, str] = {}
            candidate_missing = False
            for index in indexes:
                config = configs[index]
                image = str(config.get("image", "")).strip()
                registry = str(config.get("digest_source", "")).strip()
                try:
                    candidate_digests[index] = registry_digest_for_version(
                        image,
                        candidate.version,
                        registry=registry,
                        prefix=str(config.get("digest_tag_prefix", "")),
                    )
                except RegistryDigestNotFoundError:
                    missing.append(
                        {
                            "version": candidate.version,
                            "reason": f"missing-{registry}-digest",
                        }
                    )
                    candidate_missing = True
            if candidate_missing:
                continue
            selected_version = candidate.version
            selected_digests = candidate_digests
            break

        skipped_versions = tuple(missing) + skipped_github_release_report(
            skipped, latest_tag=selected_tag
        )
        for index in indexes:
            result = aligned[index]
            latest_digest = selected_digests[index]
            aligned[index] = replace(
                result,
                latest_version=selected_version,
                latest_digest=latest_digest,
                version_update=selected_version != result.current_version,
                digest_update=bool(result.digest_key)
                and latest_digest != result.current_digest,
                submodule_ref=submodule_ref_for_version(
                    configs[index],
                    latest_version=selected_version,
                    current_version=result.current_version,
                ),
                skipped_versions=skipped_versions,
            )
    return aligned


def monitor_configs(repo: RepoConfig) -> list[dict[str, Any]]:
    raw = repo.raw.get("upstream_monitor", [])
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list) and raw:
        return [dict(item) for item in raw if isinstance(item, dict)]
    if repo.publish_profile == "template":
        return []
    return [
        {
            "component": "aio",
            "name": repo.get("upstream_name", repo.app_slug),
            "source": "manual",
            "strategy": "notify",
            "dockerfile": "Dockerfile",
            "version_key": repo.get("upstream_version_key", "UPSTREAM_VERSION"),
            "digest_key": repo.get("upstream_digest_arg", "UPSTREAM_IMAGE_DIGEST"),
        }
    ]


def evaluate_monitor(repo: RepoConfig, config: dict[str, Any]) -> UpstreamMonitorResult:
    dockerfile = repo.path / str(config.get("dockerfile", "Dockerfile"))
    version_key = str(config.get("version_key", repo.get("upstream_version_key", "")))
    digest_key = str(config.get("digest_key", repo.get("upstream_digest_arg", "")))
    current_version = read_arg(dockerfile, version_key) if version_key else ""
    current_digest = read_arg(dockerfile, digest_key) if digest_key else ""
    source = str(config.get("source", "manual"))
    strategy = str(config.get("strategy", "notify"))
    digest_source = str(config.get("digest_source", ""))
    image = str(config.get("image", "")).strip()
    latest_version = current_version
    latest_digest = current_digest
    skipped_versions: tuple[dict[str, str], ...] = ()
    release_candidates: tuple[GitHubReleaseCandidate, ...] = ()
    skipped_release_candidates: tuple[dict[str, str], ...] = ()

    if source == "github-tags":
        latest_version = latest_github_tag(
            str(config["repo"]),
            stable_only=bool(config.get("stable_only", True)),
            strip_prefix=str(config.get("version_strip_prefix", "")),
        )
    elif source == "github-releases":
        if digest_key and digest_source and image:
            release_candidates, skipped_release_candidates = (
                github_release_candidates_result(
                    str(config["repo"]),
                    stable_only=bool(config.get("stable_only", True)),
                    prerelease_channel=str(
                        config.get("prerelease_channel", "")
                    ).strip(),
                    strip_prefix=str(config.get("version_strip_prefix", "")),
                )
            )
            latest_version = release_candidates[0].version
            skipped_versions = skipped_github_release_report(
                skipped_release_candidates,
                latest_tag=release_candidates[0].tag,
            )
        else:
            latest_version, skipped_versions = latest_github_release_result(
                str(config["repo"]),
                stable_only=bool(config.get("stable_only", True)),
                prerelease_channel=str(config.get("prerelease_channel", "")).strip(),
                strip_prefix=str(config.get("version_strip_prefix", "")),
            )
    elif source == "ghcr-tags":
        latest_version = latest_registry_tag(
            str(config["image"]),
            registry="ghcr",
            stable_only=bool(config.get("stable_only", True)),
            strip_prefix=str(config.get("version_strip_prefix", "")),
        )
    elif source == "dockerhub-tags":
        latest_version = latest_registry_tag(
            str(config["image"]),
            registry="dockerhub",
            stable_only=bool(config.get("stable_only", True)),
            strip_prefix=str(config.get("version_strip_prefix", "")),
        )
    elif source != "manual":
        raise ValueError(f"{repo.name}: unsupported upstream monitor source: {source}")

    if digest_key and digest_source and image:
        if source == "github-releases" and release_candidates:
            latest_version, latest_digest, skipped_versions = (
                release_with_resolvable_digest(
                    release_candidates,
                    skipped_release_candidates,
                    current_version=current_version,
                    current_digest=current_digest,
                    image=image,
                    registry=digest_source,
                    prefix=str(config.get("digest_tag_prefix", "")),
                )
            )
        else:
            latest_digest = registry_digest_for_version(
                image,
                latest_version,
                registry=digest_source,
                prefix=str(config.get("digest_tag_prefix", "")),
            )

    commit_key = str(config.get("commit_key", "")).strip()
    current_commit = read_arg(dockerfile, commit_key) if commit_key else ""
    latest_commit = current_commit
    if (
        commit_key
        and latest_version != current_version
        and source in {"github-releases", "github-tags"}
    ):
        selected_tag = next(
            (c.tag for c in release_candidates if c.version == latest_version),
            latest_version,
        )
        latest_commit = (
            resolve_github_commit(str(config["repo"]), selected_tag) or current_commit
        )

    return UpstreamMonitorResult(
        repo=repo.name,
        component=str(config.get("component", "aio")),
        name=str(config.get("name", repo.get("upstream_name", repo.app_slug))),
        strategy=strategy,
        source=source,
        current_version=current_version,
        latest_version=latest_version,
        current_digest=current_digest,
        latest_digest=latest_digest,
        version_update=latest_version != current_version,
        digest_update=bool(digest_key) and latest_digest != current_digest,
        dockerfile=dockerfile,
        version_key=version_key,
        digest_key=digest_key,
        commit_key=commit_key,
        current_commit=current_commit,
        latest_commit=latest_commit,
        commit_update=bool(commit_key) and latest_commit != current_commit,
        release_notes_url=str(config.get("release_notes_url", "")).strip()
        or default_release_notes_url(config),
        submodule_path=str(config.get("submodule_path", "")).strip(),
        submodule_ref=submodule_ref_for_version(
            config,
            latest_version=latest_version,
            current_version=current_version,
        ),
        skipped_versions=skipped_versions,
    )


def read_arg(dockerfile: Path, arg_name: str) -> str:
    pattern = re.compile(rf"^\s*ARG\s+{re.escape(arg_name)}=(.+?)\s*$")
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise ValueError(f"unable to find ARG {arg_name} in {dockerfile}")


def write_arg(dockerfile: Path, arg_name: str, value: str) -> None:
    pattern = re.compile(rf"^(\s*ARG\s+{re.escape(arg_name)}=).+?(\s*)$")
    changed = False
    lines: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            lines.append(f"{match.group(1)}{value}{match.group(2)}")
            changed = True
        else:
            lines.append(line)
    if not changed:
        raise ValueError(f"unable to update ARG {arg_name} in {dockerfile}")
    dockerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reset_registry_revision(repo: RepoConfig, result: UpstreamMonitorResult) -> None:
    if not result.version_update:
        return
    config = component_config(repo, result.component)
    revision_arg = str(config.get("registry_revision_arg", "") or "").strip()
    if revision_arg:
        write_arg(result.dockerfile, revision_arg, "1")


def _update_release_history_changelog(
    repo: RepoConfig, result: UpstreamMonitorResult
) -> None:
    config = component_config(repo, result.component)
    if str(config.get("release_history", "")).strip() != "github_prerelease":
        return
    changelog_name = str(config.get("release_changelog", "") or "").strip()
    if not changelog_name:
        return
    revision_arg = str(config.get("registry_revision_arg", "") or "").strip()
    revision = read_arg(result.dockerfile, revision_arg) if revision_arg else "1"
    release_suffix = str(config.get("release_suffix", "aio"))
    package_version = f"{result.latest_version}-{release_suffix}.{revision}"
    changelog = repo.path / changelog_name
    _upsert_alpha_release_section(
        changelog,
        version=package_version,
        result=result,
        customizations=_string_list(config.get("release_customization_notes", [])),
    )


def _upsert_alpha_release_section(
    changelog: Path,
    *,
    version: str,
    result: UpstreamMonitorResult,
    customizations: list[str],
) -> None:
    today = datetime.now(UTC).date().isoformat()
    heading = f"## {version} - {today}"
    lines = [
        heading,
        "",
        "### Build",
        "",
        f"- Track upstream {result.name} {result.latest_version}.",
        "- Publish Docker Hub and GHCR tags with the configured component revision tag.",
        "",
    ]
    if customizations:
        lines.extend(
            [
                "### Component Customizations",
                "",
                *[f"- {item}" for item in customizations],
                "",
            ]
        )
    section = "\n".join(lines)
    existing = changelog.read_text() if changelog.exists() else "# Alpha Changelog\n"
    lines = existing.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith(f"## {version}"):
            index += 1
            while index < len(lines) and not lines[index].startswith("## "):
                index += 1
            continue
        output.append(line)
        index += 1
    text = "\n".join(output).rstrip()
    if not text:
        text = "# Alpha Changelog"
    insert_at = text.find("\n## ")
    if insert_at == -1:
        updated = f"{text.rstrip()}\n\n{section}"
    else:
        updated = (
            f"{text[:insert_at].rstrip()}\n\n{section}\n{text[insert_at:].lstrip()}"
        )
    changelog.write_text(updated.rstrip() + "\n", encoding="utf-8")


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def update_submodule(
    repo: RepoConfig,
    config: dict[str, Any],
    result: UpstreamMonitorResult,
) -> None:
    submodule_path = result.submodule_path
    if not submodule_path:
        return
    submodule_dir = repo.path / submodule_path
    if not submodule_dir.exists():
        raise RuntimeError(
            f"{repo.name}: submodule path does not exist: {submodule_path}"
        )
    ref = result.submodule_ref
    if not ref:
        raise RuntimeError(
            f"{repo.name}: submodule ref is required for {submodule_path}"
        )
    remote = str(config.get("submodule_remote", "")).strip()
    if remote:
        run_git(submodule_dir, ["fetch", "--tags", remote, ref])
        target = "FETCH_HEAD"
    else:
        run_git(submodule_dir, ["fetch", "--tags"])
        target = ref
    run_git(submodule_dir, ["checkout", "--detach", target])


def submodule_ref_for_version(
    config: dict[str, Any],
    *,
    latest_version: str,
    current_version: str,
) -> str:
    if not str(config.get("submodule_path", "")).strip():
        return ""
    template = str(config.get("submodule_ref_template", "{version}")).strip()
    return template.format(version=latest_version, current_version=current_version)


def latest_github_tag(repo: str, *, stable_only: bool, strip_prefix: str = "") -> str:
    data = http_json(f"https://api.github.com/repos/{repo}/tags?per_page=100")
    if not isinstance(data, list):
        raise ValueError(f"unexpected GitHub tag response for {repo}")
    tags = [
        str(entry["name"])
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
    return normalize_version(
        sorted(filter_versions(tags, stable_only), key=version_sort_key)[-1],
        strip_prefix=strip_prefix,
    )


def resolve_github_commit(repo: str, tag: str) -> str:
    """Resolve a release/tag name to its commit SHA.

    Source-built app repos pin both an upstream version *label* and the upstream
    *commit* the build actually checks out, so the monitor must move the commit
    with the version or the wrapper builds stale code under a new label.
    Annotated tags are dereferenced to the underlying commit. Returns an empty
    string on any lookup failure so the caller keeps the current pin.
    """
    if not tag:
        return ""
    try:
        ref = http_json(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}")
    except Exception:  # noqa: BLE001 - a failed lookup must not break the monitor
        return ""
    if not isinstance(ref, dict) or not isinstance(ref.get("object"), dict):
        return ""
    obj = ref["object"]
    sha = str(obj.get("sha", "")).strip()
    if obj.get("type") == "tag" and sha:
        try:
            annotated = http_json(f"https://api.github.com/repos/{repo}/git/tags/{sha}")
        except Exception:  # noqa: BLE001 - fall back to the tag-object sha
            return sha
        inner = annotated.get("object") if isinstance(annotated, dict) else None
        if isinstance(inner, dict) and inner.get("sha"):
            return str(inner["sha"]).strip()
    return sha


def latest_github_release(
    repo: str,
    *,
    stable_only: bool,
    prerelease_channel: str = "",
    strip_prefix: str = "",
) -> str:
    version, _skipped = latest_github_release_result(
        repo,
        stable_only=stable_only,
        prerelease_channel=prerelease_channel,
        strip_prefix=strip_prefix,
    )
    return version


def latest_github_release_result(
    repo: str,
    *,
    stable_only: bool,
    prerelease_channel: str = "",
    strip_prefix: str = "",
) -> tuple[str, tuple[dict[str, str], ...]]:
    candidates, skipped = github_release_candidates_result(
        repo,
        stable_only=stable_only,
        prerelease_channel=prerelease_channel,
        strip_prefix=strip_prefix,
    )
    return candidates[0].version, skipped_github_release_report(
        skipped,
        latest_tag=candidates[0].tag,
    )


def github_release_candidates_result(
    repo: str,
    *,
    stable_only: bool,
    prerelease_channel: str = "",
    strip_prefix: str = "",
) -> tuple[tuple[GitHubReleaseCandidate, ...], tuple[dict[str, str], ...]]:
    data = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    if not isinstance(data, list):
        raise ValueError(f"unexpected GitHub release response for {repo}")
    candidates: list[GitHubReleaseCandidate] = []
    skipped: list[dict[str, str]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        tag = entry.get("tag_name")
        if not isinstance(tag, str) or not SEMVER_RE.match(tag):
            continue
        if stable_only and bool(entry.get("prerelease")):
            skipped.append(
                {
                    "tag": tag,
                    "version": normalize_version(tag, strip_prefix=strip_prefix),
                    "reason": "github-prerelease",
                }
            )
            continue
        if stable_only and is_prerelease_version(tag):
            skipped.append(
                {
                    "tag": tag,
                    "version": normalize_version(tag, strip_prefix=strip_prefix),
                    "reason": "version-prerelease",
                }
            )
            continue
        if prerelease_channel and not prerelease_channel_matches(
            tag, prerelease_channel
        ):
            skipped.append(
                {
                    "tag": tag,
                    "version": normalize_version(tag, strip_prefix=strip_prefix),
                    "reason": f"outside-{prerelease_channel}-channel",
                }
            )
            continue
        candidates.append(
            GitHubReleaseCandidate(
                tag=tag,
                version=normalize_version(tag, strip_prefix=strip_prefix),
            )
        )
    if not candidates:
        raise ValueError(f"no matching GitHub releases found for {repo}")
    return (
        tuple(
            sorted(
                candidates, key=lambda item: version_sort_key(item.tag), reverse=True
            )
        ),
        tuple(skipped),
    )


def skipped_github_release_report(
    skipped: tuple[dict[str, str], ...],
    *,
    latest_tag: str,
) -> tuple[dict[str, str], ...]:
    return tuple(
        {"version": item["version"], "reason": item["reason"]}
        for item in skipped
        if version_sort_key(item["tag"]) > version_sort_key(latest_tag)
    )[:10]


def release_with_resolvable_digest(
    candidates: tuple[GitHubReleaseCandidate, ...],
    skipped: tuple[dict[str, str], ...],
    *,
    current_version: str = "",
    current_digest: str = "",
    image: str,
    registry: str,
    prefix: str = "",
) -> tuple[str, str, tuple[dict[str, str], ...]]:
    missing: list[dict[str, str]] = []
    current_key = (
        version_sort_key(current_version) if SEMVER_RE.match(current_version) else None
    )
    for candidate in candidates:
        if (
            current_key is not None
            and version_sort_key(candidate.version) <= current_key
        ):
            return (
                current_version,
                current_digest,
                tuple(missing)
                + (
                    ()
                    if candidate.version == current_version
                    else (
                        {
                            "version": candidate.version,
                            "reason": "not-newer-than-current",
                        },
                    )
                )
                + skipped_github_release_report(skipped, latest_tag=candidate.tag),
            )
        try:
            digest = registry_digest_for_version(
                image,
                candidate.version,
                registry=registry,
                prefix=prefix,
            )
        except RegistryDigestNotFoundError:
            missing.append(
                {
                    "version": candidate.version,
                    "reason": f"missing-{registry}-digest",
                }
            )
            continue
        return (
            candidate.version,
            digest,
            tuple(missing)
            + skipped_github_release_report(skipped, latest_tag=candidate.tag),
        )
    versions = ", ".join(candidate.version for candidate in candidates[:10])
    raise RegistryDigestNotFoundError(
        f"unable to resolve {registry} digest for {image} using release candidates: {versions}"
    )


def latest_registry_tag(
    image: str, *, registry: str, stable_only: bool, strip_prefix: str = ""
) -> str:
    if registry == "ghcr":
        data = http_json(
            f"https://ghcr.io/v2/{image}/tags/list",
            {"Authorization": f"Bearer {ghcr_token(image)}"},
        )
        if not isinstance(data, dict):
            raise ValueError(f"unexpected GHCR tags response for {image}")
        tags = [tag for tag in data.get("tags", []) if isinstance(tag, str)]
    elif registry == "dockerhub":
        data = http_json(
            f"https://registry.hub.docker.com/v2/repositories/{image}/tags?page_size=100"
        )
        if not isinstance(data, dict):
            raise ValueError(f"unexpected Docker Hub tags response for {image}")
        tags = [
            str(item["name"])
            for item in data.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    else:
        raise ValueError(f"unsupported registry tag source: {registry}")
    return normalize_version(
        sorted(filter_versions(tags, stable_only), key=version_sort_key)[-1],
        strip_prefix=strip_prefix,
    )


def registry_digest_for_version(
    image: str, version: str, *, registry: str, prefix: str = ""
) -> str:
    candidates = version_tag_candidates(version, prefix=prefix)
    for tag in candidates:
        digest = registry_digest(image, tag, registry=registry)
        if digest:
            return digest
    raise RegistryDigestNotFoundError(
        f"unable to resolve {registry} digest for {image} using tags: {', '.join(candidates)}"
    )


def registry_digest(image: str, tag: str, *, registry: str) -> str | None:
    if registry == "ghcr":
        url = f"https://ghcr.io/v2/{image}/manifests/{tag}"
        token = ghcr_token(image)
    elif registry == "dockerhub":
        url = f"https://registry-1.docker.io/v2/{image}/manifests/{tag}"
        token = dockerhub_token(image)
    else:
        raise ValueError(f"unsupported digest source: {registry}")
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "Accept": ",".join(
                [
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                ]
            ),
            "Authorization": f"Bearer {token}",
            "User-Agent": "aio-fleet",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            return response.headers.get("docker-content-digest", "").strip() or None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ValueError(
            f"HTTP error while resolving {registry} digest for {image}:{tag}: {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(
            f"network error while resolving {registry} digest for {image}:{tag}: {exc.reason}"
        ) from exc


def ghcr_token(image: str) -> str:
    data = http_json(f"https://ghcr.io/token?scope=repository:{image}:pull")
    if not isinstance(data, dict) or not data.get("token"):
        raise ValueError(f"unable to resolve GHCR token for {image}")
    return str(data["token"])


def dockerhub_token(image: str) -> str:
    scope = urllib.parse.quote(f"repository:{image}:pull")
    data = http_json(
        f"https://auth.docker.io/token?service=registry.docker.io&scope={scope}"
    )
    if not isinstance(data, dict) or not data.get("token"):
        raise ValueError(f"unable to resolve Docker Hub token for {image}")
    return str(data["token"])


def http_json(url: str, headers: dict[str, str] | None = None) -> object:
    request_headers = {
        "Accept": "application/vnd.github+json, application/json",
        "User-Agent": "aio-fleet",
        **(headers or {}),
    }
    token = github_token()
    hostname = urllib.parse.urlparse(url).hostname
    if (
        token
        and hostname == "api.github.com"
        and "Authorization" not in request_headers
    ):
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=request_headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                return json.load(response)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 or attempt == 2:
                raise ValueError(
                    f"HTTP error while requesting {url}: {exc.code} {exc.reason}"
                ) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == 2:
                raise ValueError(
                    f"network error while requesting {url}: {exc.reason}"
                ) from exc
        time.sleep(2**attempt)
    raise ValueError(f"network error while requesting {url}: {last_error}")


@lru_cache(maxsize=1)
def github_token() -> str:
    for env_name in (
        "AIO_FLEET_UPSTREAM_TOKEN",
        "APP_TOKEN",
        "AIO_FLEET_CHECK_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        token = os.environ.get(env_name, "").strip()
        if token:
            return token
    gh = shutil.which("gh")
    if gh is None:
        return ""
    result = subprocess.run(  # nosec B603
        [gh, "auth", "token"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def github_cli_env() -> dict[str, str] | None:
    token = github_token()
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def filter_versions(values: list[str], stable_only: bool) -> list[str]:
    candidates: list[str] = []
    for value in values:
        if not SEMVER_RE.match(value):
            continue
        if stable_only and is_prerelease_version(value):
            continue
        candidates.append(value)
    if not candidates:
        raise ValueError("no semver-like upstream versions found")
    return candidates


def parse_version(
    value: str,
) -> tuple[int, int, int, bool, tuple[tuple[int, object], ...]]:
    match = SEMVER_RE.match(value)
    if not match:
        raise ValueError(f"unsupported version format: {value}")
    suffix = match.group("prerelease") or ""
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        is_prerelease_suffix(suffix),
        prerelease_sort_key(suffix),
    )


def is_prerelease_version(value: str) -> bool:
    match = SEMVER_RE.match(value)
    if not match:
        return False
    return is_prerelease_suffix(match.group("prerelease") or "")


def is_prerelease_suffix(suffix: str) -> bool:
    if not suffix:
        return False
    lowered = suffix.lower()
    if lowered in STABLE_MAINTENANCE_SUFFIXES:
        return False
    if re.fullmatch(r"hotfix\.[0-9]+", lowered):
        return False
    label = suffix.split(".", 1)[0].split("-", 1)[0].lower()
    if label in PRERELEASE_SUFFIXES:
        return True
    # Unknown suffixes stay prerelease-like until a repo needs an explicit stable allowlist entry.
    return True


def prerelease_channel_matches(value: str, channel: str) -> bool:
    match = SEMVER_RE.match(value)
    if not match:
        return False
    suffix = match.group("prerelease") or ""
    if not suffix:
        return False
    label = suffix.split(".", 1)[0].split("-", 1)[0].lower()
    return label == channel.lower()


def prerelease_sort_key(prerelease: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for item in prerelease.split("."):
        if not item:
            continue
        parts.append((0, int(item)) if item.isdigit() else (1, item))
    return tuple(parts)


def version_sort_key(
    value: str,
) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    major, minor, patch, prerelease, prerelease_key = parse_version(value)
    return (major, minor, patch, 0 if prerelease else 1, prerelease_key)


def normalize_version(value: str, *, strip_prefix: str = "") -> str:
    if strip_prefix and value.startswith(strip_prefix):
        return value[len(strip_prefix) :]
    return value


def version_tag_candidates(version: str, *, prefix: str = "") -> list[str]:
    candidates = [version]
    if prefix:
        candidates.append(f"{prefix}{version.removeprefix(prefix)}")
    if version.startswith("v"):
        candidates.append(version[1:])
    else:
        candidates.append(f"v{version}")
    return list(dict.fromkeys(candidates))


def default_release_notes_url(config: dict[str, Any]) -> str:
    upstream_repo = str(config.get("repo", "")).strip()
    if upstream_repo:
        return f"https://github.com/{upstream_repo}/releases"
    return ""


def result_dict(result: UpstreamMonitorResult) -> dict[str, object]:
    blocked = bool(getattr(result, "blocked", False))
    data: dict[str, object] = {
        "repo": result.repo,
        "component": result.component,
        "name": result.name,
        "strategy": result.strategy,
        "source": result.source,
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "current_digest": result.current_digest,
        "latest_digest": result.latest_digest,
        "version_update": result.version_update,
        "digest_update": result.digest_update,
        "updates_available": result.updates_available,
        "dockerfile": str(result.dockerfile),
        "release_notes_url": result.release_notes_url,
        "state": (
            "blocked"
            if blocked
            else ("updates" if result.updates_available else "current")
        ),
    }
    if blocked:
        data["blocked"] = True
        data["blocked_reason"] = getattr(result, "blocked_reason", "")
        data["next_action"] = getattr(result, "next_action", "")
    skipped_versions = getattr(result, "skipped_versions", ())
    if skipped_versions:
        data["skipped_versions"] = list(skipped_versions)
    submodule_path = getattr(result, "submodule_path", "")
    if submodule_path:
        data["submodule_path"] = submodule_path
        data["submodule_ref"] = getattr(result, "submodule_ref", "")
    return data


def create_or_update_upstream_pr(
    repo: RepoConfig,
    results: list[UpstreamMonitorResult],
    *,
    dry_run: bool,
    post_check: bool,
) -> dict[str, object]:
    changed = [
        result
        for result in results
        if result.updates_available and result.strategy == "pr" and not result.blocked
    ]
    if not changed:
        if any(result.blocked for result in results):
            reason = "blocked-upstream-update"
        elif any(result.updates_available for result in results):
            reason = "no-pr-strategy-updates"
        else:
            reason = "no-updates"
        action: dict[str, object] = {
            "repo": repo.name,
            "action": "skipped",
            "reason": reason,
        }
        blocked = [result for result in results if result.blocked]
        if blocked:
            action["blockers"] = [result_dict(result) for result in blocked]
        return action
    branch = upstream_branch(repo, changed)
    title = upstream_title(repo, changed)
    configured_paths = repo.list_value("upstream_commit_paths")
    commit_paths = sorted(_upstream_commit_paths(repo, changed, configured_paths))
    body = upstream_body(repo, changed, changed_paths=commit_paths)
    if dry_run:
        payload: dict[str, object] = {
            "repo": repo.name,
            "action": "would-create-pr",
            "branch": branch,
            "title": title,
            "paths": commit_paths,
        }
        if post_check:
            payload["check_payload"] = check_run_payload(
                repo,
                sha="0" * 40,
                event="pull_request",
                status="queued",
                summary="Queued from aio-fleet upstream monitor",
            )
        return payload

    committed = commit_paths_to_branch(
        repo,
        branch=branch,
        paths=commit_paths,
        message=title,
        base="main",
        require_verified=True,
    )
    if committed.action == "no-diff":
        return {"repo": repo.name, "action": "skipped", "reason": "no-diff"}
    pr_url = upsert_pr(repo, branch=branch, title=title, body=body)
    superseded = close_superseded_upstream_prs(
        repo, current_branch=branch, current_pr_url=pr_url
    )
    if post_check:
        upsert_check_run(
            repo,
            sha=committed.sha,
            event="pull_request",
            status="queued",
            summary="Queued from aio-fleet upstream monitor",
        )
    return {
        "repo": repo.name,
        "action": "upserted-pr",
        "branch": branch,
        "url": pr_url,
        "sha": committed.sha,
        "commit_method": committed.method,
        "verified": committed.verified,
        "superseded": superseded,
    }


def _upstream_commit_paths(
    repo: RepoConfig,
    changed: list[UpstreamMonitorResult],
    configured_paths: list[str],
) -> set[str]:
    paths = set(
        configured_paths
        or {str(result.dockerfile.relative_to(repo.path)) for result in changed}
    )
    for result in changed:
        config = component_config(repo, result.component)
        changelog = str(config.get("release_changelog", "") or "").strip()
        if changelog:
            paths.add(changelog)
    return paths


def upstream_branch(repo: RepoConfig, results: list[UpstreamMonitorResult]) -> str:
    if len(results) == 1:
        version = results[0].latest_version.replace("/", "-")
        return f"codex/upstream-{repo.name}-{version}"
    return f"codex/upstream-{repo.name}-pins"


def upstream_title(repo: RepoConfig, results: list[UpstreamMonitorResult]) -> str:
    if len(results) == 1:
        result = results[0]
        return f"chore(sync): bump {result.name.lower()} to {result.latest_version}"
    return f"chore(sync): update upstream pins for {repo.app_slug}"


def upstream_body(
    repo: RepoConfig,
    results: list[UpstreamMonitorResult],
    *,
    changed_paths: list[str] | None = None,
) -> str:
    changed_paths = sorted(
        changed_paths
        or repo.list_value("upstream_commit_paths")
        or {str(result.dockerfile.relative_to(repo.path)) for result in results}
    )
    safety = assess_expected_update(
        repo,
        results,
        changed_files=changed_paths,
        inspect_release_notes=False,
    )
    lines = [
        "## Summary",
        f"- Updates upstream pins for `{repo.name}`.",
        "",
        "## What changed",
    ]
    for result in results:
        detail = f"{result.name}: {result.current_version} -> {result.latest_version}"
        if result.digest_update:
            detail += " plus image digest refresh"
        lines.append(f"- {detail}")
        if result.release_notes_url:
            lines.append(f"- Release notes: {result.release_notes_url}")
    lines.extend(
        [
            "- Source repo paths reviewed/generated:",
            *[f"  - `{path}`" for path in changed_paths],
            "",
            "",
            "## Why",
            "- Keeps the AIO wrapper aligned with upstream while preserving human review.",
            "- Source repo changes are validated here first; catalog sync follows the validated source repo and never starts in `awesome-unraid`.",
            "",
            "## Validation",
            "- Generated by `aio-fleet upstream monitor`; central checks should run on this PR.",
            "- The generated commit must be verified/signed before branch protection allows merge.",
            "",
            "## Safety assessment",
            *render_safety_summary(safety),
            "- Dashboard safety state is authoritative after this PR and its checks are visible to `aio-fleet`.",
        ]
    )
    body = "\n".join(lines)
    assert_public_text(body, context="upstream PR body")
    return body


def run_git(
    cwd: Path, args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    git = required_executable("git")
    result = subprocess.run(  # nosec B603
        [git, *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def upsert_pr(repo: RepoConfig, *, branch: str, title: str, body: str) -> str:
    assert_public_text(title, context="upstream PR title")
    assert_public_text(body, context="upstream PR body")
    gh = required_executable("gh")
    env = github_cli_env()
    existing = subprocess.run(  # nosec B603
        [
            gh,
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--head",
            branch,
            "--base",
            "main",
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        cwd=repo.path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if existing.returncode != 0:
        raise RuntimeError(existing.stderr.strip() or "unable to inspect PRs")
    url = existing.stdout.strip()
    if url:
        edit = subprocess.run(  # nosec B603
            [gh, "pr", "edit", url, "--title", title, "--body", body],
            cwd=repo.path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if edit.returncode != 0:
            raise RuntimeError(edit.stderr.strip() or "unable to update PR")
        return url
    created = subprocess.run(  # nosec B603
        [
            gh,
            "pr",
            "create",
            "--repo",
            repo.github_repo,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=repo.path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "unable to create PR")
    return created.stdout.strip()


def close_superseded_upstream_prs(
    repo: RepoConfig, *, current_branch: str, current_pr_url: str
) -> list[int]:
    gh = required_executable("gh")
    env = github_cli_env()
    listed = subprocess.run(  # nosec B603
        [
            gh,
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--state",
            "open",
            "--json",
            "number,headRefName",
        ],
        cwd=repo.path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return []
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return []
    prefix = f"codex/upstream-{repo.name}-"
    closed: list[int] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        branch = str(pr.get("headRefName") or "")
        if not branch.startswith(prefix) or branch == current_branch:
            continue
        number = int(pr.get("number") or 0)
        if not number:
            continue
        message = (
            f"Superseded by {current_pr_url}. "
            "aio-fleet keeps one active upstream update PR per generated branch."
        )
        closed_pr = subprocess.run(  # nosec B603
            [gh, "pr", "close", str(number), "--comment", message],
            cwd=repo.path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if closed_pr.returncode == 0:
            closed.append(number)
    return closed


def required_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} CLI is required")
    return path
