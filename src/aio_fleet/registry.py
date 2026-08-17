from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from aio_fleet.changelog import component_config
from aio_fleet.cleanup import RETIRED_SHARED_PATHS
from aio_fleet.manifest import RepoConfig
from aio_fleet.release import (
    find_release_target_commit,
    git,
    git_is_ancestor,
    latest_changelog_version,
    latest_component_changelog_version,
    read_upstream_version,
)

REGISTRY_IMAGETOOLS_TIMEOUT_SECONDS = int(
    os.environ.get("AIO_FLEET_REGISTRY_INSPECT_TIMEOUT", "20")
)
SHA_TAG_RE = re.compile(r":sha-[0-9a-f]{40}(?::|\b)")
_REGISTRY_TAG_SUCCESS_CACHE: set[tuple[str, int, tuple[tuple[str, str], ...]]] = set()


@dataclass(frozen=True)
class RegistryTagSet:
    dockerhub: list[str]
    ghcr: list[str]
    upstream_version: str
    release_package_tag: str

    @property
    def all_tags(self) -> list[str]:
        return [*self.dockerhub, *self.ghcr]


def compute_registry_tags(
    repo: RepoConfig,
    *,
    sha: str,
    component: str = "aio",
    ghcr_image_name: str | None = None,
    include_sha_tag: bool | None = None,
) -> RegistryTagSet:
    image_name = _component_image_name(repo, component)
    dockerhub_image = image_name.lower()
    ghcr_image = (ghcr_image_name or f"ghcr.io/{image_name}").lower()
    upstream_version = _read_component_upstream_version(repo, component)
    release_package_tag = _release_package_tag(repo, sha=sha, component=component)
    version_tags_allowed = _version_tags_allowed(
        repo, component=component, release_package_tag=release_package_tag
    )
    upstream_version_tag_allowed = _upstream_version_tag_allowed(
        repo,
        component=component,
        release_package_tag=release_package_tag,
        version_tags_allowed=version_tags_allowed,
    )
    include_upstream_version_tag = _component_bool(
        repo, component, "include_upstream_version_tag", True
    )
    if include_sha_tag is None:
        include_sha_tag = _component_bool(repo, component, "include_sha_tag", True)

    dockerhub_tags = []
    ghcr_tags = []
    if version_tags_allowed:
        dockerhub_tags.extend(
            f"{dockerhub_image}:{tag}"
            for tag in _component_floating_tags(repo, component)
        )
        ghcr_tags.extend(
            f"{ghcr_image}:{tag}" for tag in _component_floating_tags(repo, component)
        )
    if (
        upstream_version_tag_allowed
        and include_upstream_version_tag
        and upstream_version
    ):
        dockerhub_tags.append(f"{dockerhub_image}:{upstream_version}")
        ghcr_tags.append(f"{ghcr_image}:{upstream_version}")
    if version_tags_allowed and release_package_tag:
        dockerhub_tags.append(f"{dockerhub_image}:{release_package_tag}")
        ghcr_tags.append(f"{ghcr_image}:{release_package_tag}")
    if include_sha_tag:
        dockerhub_tags.append(
            f"{dockerhub_image}:{_component_sha_tag(repo, component, sha)}"
        )
        ghcr_tags.append(f"{ghcr_image}:{_component_sha_tag(repo, component, sha)}")
    return RegistryTagSet(
        dockerhub=dockerhub_tags,
        ghcr=ghcr_tags,
        upstream_version=upstream_version,
        release_package_tag=release_package_tag,
    )


def registry_sha_tag_required(
    repo: RepoConfig, *, component: str = "aio", sha: str
) -> bool:
    if not _component_bool(repo, component, "include_sha_tag", True):
        return False
    release_target_commit = _registry_sha_release_target_commit(repo, component)
    if not release_target_commit:
        return True
    if release_target_commit == sha:
        return True
    try:
        if not git_is_ancestor(repo.path, release_target_commit, sha):
            return True
        changed_paths = _changed_paths_between(repo.path, release_target_commit, sha)
    except (Exception, SystemExit):
        return True
    if not changed_paths:
        return False
    if _sha_tag_skip_paths_only(repo, changed_paths):
        return False
    if _component_change_unrelated(repo, component, changed_paths):
        return False
    return True


def registry_failures_are_sha_only(failures: list[str]) -> bool:
    return bool(failures) and all(
        SHA_TAG_RE.search(str(failure)) for failure in failures
    )


def component_registry_release_tag(repo: RepoConfig, component: str = "aio") -> str:
    config = component_config(repo, component)
    revision_arg = str(config.get("registry_revision_arg", "") or "").strip()
    if not revision_arg:
        return ""
    upstream_version = _read_component_upstream_version(repo, component)
    if not upstream_version:
        return ""
    revision = _read_component_arg(repo, component, revision_arg)
    if not revision:
        return ""
    release_suffix = str(config.get("release_suffix", "aio"))
    return f"{upstream_version}-{release_suffix}.{revision}"


def _component_release_target_commit(repo: RepoConfig, component: str) -> str:
    config = component_config(repo, component)
    if str(config.get("release_policy", "")).strip() == "registry_only":
        return ""
    upstream_version = _read_component_upstream_version(repo, component)
    if not upstream_version:
        return ""
    release_suffix = str(config.get("release_suffix", "aio"))
    changelog_path = repo.path / "CHANGELOG.md"
    try:
        if repo.publish_profile == "changelog-version":
            changelog_version = latest_changelog_version(changelog_path)
        else:
            changelog_version = latest_component_changelog_version(
                changelog_path,
                upstream_version=upstream_version,
                suffix=release_suffix,
            )
        return find_release_target_commit(repo.path, changelog_version)
    except (Exception, SystemExit):
        return ""


def _registry_sha_release_target_commit(repo: RepoConfig, component: str) -> str:
    config = component_config(repo, component)
    if str(config.get("release_policy", "")).strip() != "registry_only":
        return _component_release_target_commit(repo, component)
    release_package_tag = component_registry_release_tag(repo, component)
    if not release_package_tag:
        return ""
    return _registry_only_release_target_commit(
        repo, component=component, release_package_tag=release_package_tag
    )


def _version_tags_allowed(
    repo: RepoConfig, *, component: str, release_package_tag: str
) -> bool:
    config = component_config(repo, component)
    if str(config.get("release_policy", "")).strip() == "registry_only":
        return bool(release_package_tag)
    if repo.publish_profile == "upstream-aio-track":
        return bool(release_package_tag)
    return True


def _upstream_version_tag_allowed(
    repo: RepoConfig,
    *,
    component: str,
    release_package_tag: str,
    version_tags_allowed: bool,
) -> bool:
    if not version_tags_allowed:
        return False
    config = component_config(repo, component)
    release_suffix = str(config.get("release_suffix", "") or "").strip()
    if release_suffix:
        return bool(release_package_tag)
    return True


def verify_registry_tags(
    tags: list[str],
    *,
    env: Mapping[str, str] | None = None,
    dockerhub_attempts: int = 8,
) -> list[str]:
    docker = shutil.which("docker")
    if docker is None:
        return ["docker CLI is required to verify registry tags"]
    failures: list[str] = []
    env_key = _registry_verify_env_key(env)
    for tag in tags:
        cache_key = (tag, dockerhub_attempts, env_key)
        if cache_key in _REGISTRY_TAG_SUCCESS_CACHE:
            continue
        failure = (
            _verify_dockerhub_tag(docker, tag, env=env, attempts=dockerhub_attempts)
            if _is_dockerhub_tag(tag)
            else _verify_with_docker_imagetools(docker, tag, env=env)
        )
        if failure:
            failures.append(failure)
        else:
            _REGISTRY_TAG_SUCCESS_CACHE.add(cache_key)
    return failures


def _registry_verify_env_key(
    env: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if env is None:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in env.items()))


def delete_dockerhub_tags(
    *,
    image: str,
    tags: list[str],
    username: str,
    token: str,
    required_substring: str = "",
    dry_run: bool = False,
) -> list[dict[str, str]]:
    parsed = _dockerhub_image_parts(image)
    if parsed is None:
        raise ValueError(f"{image}: unsupported Docker Hub image format")
    namespace, repository = parsed
    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_repository = urllib.parse.quote(repository, safe="")
    cleaned_tags = _clean_tag_list(tags)
    if not cleaned_tags:
        raise ValueError("at least one Docker Hub tag is required")

    required = required_substring.strip()
    if required:
        for tag in cleaned_tags:
            if required not in tag:
                raise ValueError(
                    f"{tag}: refusing to delete tag without required substring "
                    f"{required!r}"
                )

    if dry_run:
        return [{"tag": tag, "state": "would-delete"} for tag in cleaned_tags]

    if not username or not token:
        raise ValueError("DOCKERHUB_USERNAME and DOCKERHUB_DELETE_TOKEN are required")

    auth_token = _dockerhub_login_token(username=username, token=token)
    results: list[dict[str, str]] = []
    for tag in cleaned_tags:
        quoted_tag = urllib.parse.quote(tag, safe="")
        url = (
            "https://hub.docker.com/v2/"
            f"namespaces/{quoted_namespace}/repositories/{quoted_repository}/tags/{quoted_tag}"
        )
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {auth_token}"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
                if response.status in {200, 202, 204}:
                    results.append({"tag": tag, "state": "deleted"})
                else:
                    results.append(
                        {"tag": tag, "state": f"unexpected:{response.status}"}
                    )
        except urllib.error.HTTPError as error:
            if error.code == 404:
                results.append({"tag": tag, "state": "missing"})
            elif error.code == 403:
                raise RuntimeError(
                    f"{tag}: Docker Hub delete forbidden for "
                    f"{namespace}/{repository}; the Docker Hub token "
                    "authenticated but lacks tag delete/admin permission"
                ) from error
            else:
                raise RuntimeError(
                    f"{tag}: Docker Hub delete failed: HTTP {error.code}: "
                    f"{error.reason}"
                ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"{tag}: Docker Hub delete failed: {error.reason}"
            ) from error
    return results


def dockerhub_auth_preflight_failure(*, username: str, token: str) -> str | None:
    if not username or not token:
        return "DOCKERHUB_USERNAME and Docker Hub token are required"
    try:
        _dockerhub_login_token(username=username, token=token)
    except RuntimeError as exc:
        return str(exc)
    return None


def dockerhub_delete_scope_preflight_failure(
    *,
    image: str,
    username: str,
    token: str,
    probe_tag: str | None = None,
) -> str | None:
    parsed = _dockerhub_image_parts(image)
    if parsed is None:
        return f"{image}: unsupported Docker Hub image format"
    if not username or not token:
        return "DOCKERHUB_USERNAME and DOCKERHUB_DELETE_TOKEN are required"

    namespace, repository = parsed
    tag = probe_tag or f"aio-fleet-preflight-missing-{uuid.uuid4().hex}"
    try:
        auth_token = _dockerhub_login_token(username=username, token=token)
    except RuntimeError as exc:
        return str(exc)
    request = urllib.request.Request(
        _dockerhub_tag_delete_url(namespace, repository, tag),
        headers={"Authorization": f"Bearer {auth_token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
            if response.status in {200, 202, 204, 404}:
                return None
            return f"{image}: Docker Hub delete probe returned HTTP {response.status}"
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        if error.code == 403:
            return (
                f"{image}: Docker Hub delete forbidden; "
                "DOCKERHUB_DELETE_TOKEN must have tag delete/admin permission"
            )
        return f"{image}: Docker Hub delete probe failed: HTTP {error.code}: {error.reason}"
    except urllib.error.URLError as error:
        return f"{image}: Docker Hub delete probe failed: {error.reason}"


def _dockerhub_tag_delete_url(namespace: str, repository: str, tag: str) -> str:
    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_repository = urllib.parse.quote(repository, safe="")
    quoted_tag = urllib.parse.quote(tag, safe="")
    return (
        "https://hub.docker.com/v2/"
        f"namespaces/{quoted_namespace}/repositories/{quoted_repository}/tags/{quoted_tag}"
    )


def _verify_with_docker_imagetools(
    docker: str, tag: str, *, env: Mapping[str, str] | None = None
) -> str | None:
    try:
        result = subprocess.run(  # nosec B603
            [docker, "buildx", "imagetools", "inspect", tag],
            check=False,
            text=True,
            capture_output=True,
            env=env,
            timeout=REGISTRY_IMAGETOOLS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"{tag}: docker buildx imagetools inspect timed out after "
            f"{REGISTRY_IMAGETOOLS_TIMEOUT_SECONDS}s"
        )
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return f"{tag}: {detail or 'inspect failed'}"


def _is_dockerhub_tag(tag: str) -> bool:
    image = tag.rsplit(":", 1)[0] if ":" in tag else tag
    first = image.split("/", 1)[0]
    return first in {"docker.io", "index.docker.io"} or "." not in first


def _verify_dockerhub_tag(
    docker: str,
    tag: str,
    *,
    env: Mapping[str, str] | None = None,
    attempts: int = 8,
) -> str | None:
    docker_failure = _verify_with_docker_imagetools(docker, tag, env=env)
    if docker_failure is None:
        return None
    if "timed out after" in docker_failure:
        return docker_failure

    parsed = _dockerhub_tag_parts(tag)
    if parsed is None:
        return f"{tag}: unsupported Docker Hub tag format"
    namespace, repository, tag_name = parsed
    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_repository = urllib.parse.quote(repository, safe="")
    quoted_tag = urllib.parse.quote(tag_name, safe="")
    url = (
        "https://hub.docker.com/v2/repositories/"
        f"{quoted_namespace}/{quoted_repository}/tags/{quoted_tag}"
    )
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:  # nosec B310
                if response.status == 200:
                    try:
                        json.load(response)
                    except (
                        json.JSONDecodeError,
                        UnicodeDecodeError,
                        OSError,
                        ValueError,
                    ) as error:
                        last_error = f"invalid Docker Hub JSON response: {error}"
                        if attempt < attempts:
                            time.sleep(2 * attempt)
                        continue
                    return None
                last_error = f"unexpected status {response.status}"
        except urllib.error.HTTPError as error:
            if error.code == 404:
                last_error = "tag not found on Docker Hub"
            else:
                last_error = f"HTTP {error.code}: {error.reason}"
        except urllib.error.URLError as error:
            last_error = str(error.reason)
        if attempt < attempts:
            time.sleep(2 * attempt)
    if last_error == "tag not found on Docker Hub":
        return f"{tag}: {last_error}"
    return f"{tag}: Docker Hub tag lookup failed: {last_error or 'unknown error'}"


def _dockerhub_tag_parts(tag: str) -> tuple[str, str, str] | None:
    if ":" not in tag:
        return None
    image, tag_name = tag.rsplit(":", 1)
    parsed = _dockerhub_image_parts(image)
    if parsed is None:
        return None
    namespace, repository = parsed
    if not tag_name:
        return None
    return namespace, repository, tag_name


_DOCKERHUB_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def _dockerhub_image_parts(image: str) -> tuple[str, str] | None:
    parts = image.split("/")
    if parts and parts[0] in {"docker.io", "index.docker.io"}:
        parts = parts[1:]
    if len(parts) == 1:
        namespace, repository = "library", parts[0]
    elif len(parts) == 2:
        namespace, repository = parts
    else:
        return None
    if not namespace or not repository or ":" in repository:
        return None
    if (
        _DOCKERHUB_NAME_PATTERN.fullmatch(namespace) is None
        or _DOCKERHUB_NAME_PATTERN.fullmatch(repository) is None
    ):
        return None
    return namespace, repository


def _clean_tag_list(tags: list[str]) -> list[str]:
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    return list(dict.fromkeys(cleaned))


def _dockerhub_login_token(*, username: str, token: str) -> str:
    payload = json.dumps({"identifier": username, "secret": token}).encode()
    request = urllib.request.Request(
        "https://hub.docker.com/v2/auth/token",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
            body = json.load(response)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError(
            f"Docker Hub login returned invalid JSON: {error}"
        ) from error
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Docker Hub login failed: HTTP {error.code}: {error.reason}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Docker Hub login failed: {error.reason}") from error
    auth_token = str(body.get("access_token", "") or body.get("token", "") or "")
    if not auth_token:
        raise RuntimeError("Docker Hub login did not return a token")
    return auth_token


def _component_image_name(repo: RepoConfig, component: str) -> str:
    components = repo.raw.get("components")
    if isinstance(components, dict):
        config = components.get(component)
        if isinstance(config, dict) and config.get("image_name"):
            return str(config["image_name"])
    return repo.image_name


def _component_floating_tags(repo: RepoConfig, component: str) -> list[str]:
    tags = component_config(repo, component).get("floating_tags", ["latest"])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        return ["latest"]
    cleaned = [str(tag).strip() for tag in tags if str(tag).strip()]
    return list(dict.fromkeys(cleaned)) or ["latest"]


def _component_sha_tag(repo: RepoConfig, component: str, sha: str) -> str:
    prefix = str(component_config(repo, component).get("sha_tag_prefix", "sha-"))
    return f"{prefix}{sha}"


def _component_bool(repo: RepoConfig, component: str, key: str, default: bool) -> bool:
    value = component_config(repo, component).get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _read_component_upstream_version(repo: RepoConfig, component: str) -> str:
    try:
        config = component_config(repo, component)
        return read_upstream_version(
            repo.path / str(config.get("dockerfile", "Dockerfile")),
            repo.path / str(config.get("upstream_config", "upstream.toml")),
            version_key=str(config.get("upstream_version_key", "UPSTREAM_VERSION")),
        )
    except (Exception, SystemExit):
        return ""


def _read_component_arg(repo: RepoConfig, component: str, arg_name: str) -> str:
    try:
        config = component_config(repo, component)
        dockerfile = repo.path / str(config.get("dockerfile", "Dockerfile"))
        pattern = re.compile(rf"^ARG {re.escape(arg_name)}=(.+)$")
        for line in dockerfile.read_text().splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(1).split("@", 1)[0]
    except (Exception, SystemExit):
        return ""
    return ""


def _release_package_tag(repo: RepoConfig, *, sha: str, component: str) -> str:
    config = component_config(repo, component)
    release_suffix = str(config.get("release_suffix", "aio"))
    if str(config.get("release_policy", "")).strip() == "registry_only":
        release_package_tag = component_registry_release_tag(repo, component)
        if not release_package_tag:
            return ""
        release_target_commit = _registry_only_release_target_commit(
            repo, component=component, release_package_tag=release_package_tag
        )
        if not _release_tag_sha_allowed(
            repo,
            release_target_commit,
            sha,
            component=component,
            release_suffix=release_suffix,
        ):
            return ""
        return release_package_tag
    upstream_version = _read_component_upstream_version(repo, component)
    if not upstream_version:
        return ""
    changelog_path = repo.path / "CHANGELOG.md"
    try:
        if repo.publish_profile == "changelog-version":
            changelog_version = latest_changelog_version(changelog_path)
        else:
            changelog_version = latest_component_changelog_version(
                changelog_path,
                upstream_version=upstream_version,
                suffix=release_suffix,
            )
    except (Exception, SystemExit):
        return ""
    try:
        release_target_commit = find_release_target_commit(repo.path, changelog_version)
    except (Exception, SystemExit):
        release_target_commit = ""
    if not _release_tag_sha_allowed(
        repo,
        release_target_commit,
        sha,
        component=component,
        release_suffix=release_suffix,
    ):
        return ""

    optional_v = "v?" if not upstream_version.startswith("v") else ""
    match = re.match(
        rf"^(?P<vprefix>{optional_v}){re.escape(upstream_version)}-"
        rf"{re.escape(release_suffix)}\.(\d+)$",
        changelog_version,
    )
    if not match:
        return changelog_version if repo.publish_profile == "changelog-version" else ""

    revision = match.group(2)
    version_prefix = match.group("vprefix")
    if repo.publish_profile == "upstream-aio-track":
        return f"{version_prefix}{upstream_version}-{release_suffix}.{revision}"
    return changelog_version


def _registry_only_release_target_commit(
    repo: RepoConfig, *, component: str, release_package_tag: str
) -> str:
    try:
        return find_release_target_commit(repo.path, release_package_tag)
    except (Exception, SystemExit):
        pass
    if str(
        component_config(repo, component).get("release_history", "")
    ).strip() == "github_prerelease" and not _registry_only_prerelease_version_matches(
        repo, component=component, release_package_tag=release_package_tag
    ):
        return ""
    config = component_config(repo, component)
    tracked_paths = [
        str(config.get("dockerfile", "Dockerfile")),
        str(config.get("upstream_config", "upstream.toml")),
        str(config.get("release_changelog", "CHANGELOG.md")),
    ]
    try:
        return git(
            repo.path, "log", "-n", "1", "--format=%H", "--", *tracked_paths
        ).strip()
    except (Exception, SystemExit):
        return ""


def _registry_only_prerelease_version_matches(
    repo: RepoConfig, *, component: str, release_package_tag: str
) -> bool:
    config = component_config(repo, component)
    if str(config.get("release_history", "")).strip() != "github_prerelease":
        return False
    upstream_version = _read_component_upstream_version(repo, component)
    if not upstream_version:
        return False
    changelog = repo.path / str(config.get("release_changelog", "CHANGELOG.md"))
    release_suffix = str(config.get("release_suffix", "aio"))
    try:
        changelog_version = latest_component_changelog_version(
            changelog,
            upstream_version=upstream_version,
            suffix=release_suffix,
        )
    except (Exception, SystemExit):
        return False
    return _normalized_version(changelog_version) == _normalized_version(
        release_package_tag
    )


def _normalized_version(value: str) -> str:
    return value[1:] if value.startswith("v") else value


_RELEASE_FORMAT_SUBJECT = re.compile(
    r"^chore\(release\): format .+ changelog(?: \(#\d+\))?$"
)
_RELEASE_CLEANUP_SUBJECT = re.compile(r"^chore\(cleanup\): .+(?: \(#\d+\))?$")


def _release_tag_sha_allowed(
    repo: RepoConfig,
    release_target_commit: str,
    sha: str,
    *,
    component: str = "aio",
    release_suffix: str = "aio",
) -> bool:
    if release_target_commit == sha:
        return True
    try:
        if not git_is_ancestor(repo.path, release_target_commit, sha):
            return False
        subjects = git(
            repo.path, "log", "--format=%s", f"{release_target_commit}..{sha}"
        )
        changed_files = git(
            repo.path, "diff", "--name-only", f"{release_target_commit}..{sha}"
        )
        changed_status = git(
            repo.path, "diff", "--name-status", f"{release_target_commit}..{sha}"
        )
    except (Exception, SystemExit):
        return False

    subject_lines = [
        subject.strip() for subject in subjects.splitlines() if subject.strip()
    ]
    changed_paths = [
        path.strip() for path in changed_files.splitlines() if path.strip()
    ]
    non_publish_patterns = _non_publish_patterns(repo)
    if (
        not _component_bool(repo, component, "include_sha_tag", True)
        and non_publish_patterns
        and changed_paths
        and all(
            _matches_release_pattern(path, non_publish_patterns)
            for path in changed_paths
        )
    ):
        return True
    if not subject_lines:
        return False
    if _cleanup_followup_allowed(
        repo,
        subject_lines=subject_lines,
        paths=changed_paths,
        name_status_lines=[
            line.strip() for line in changed_status.splitlines() if line.strip()
        ],
    ):
        return True
    if all(_RELEASE_FORMAT_SUBJECT.match(subject) for subject in subject_lines):
        return changed_paths == ["CHANGELOG.md"]
    allowed_paths = _component_release_followup_paths(repo, component)
    return set(changed_paths).issubset(allowed_paths) and all(
        _release_followup_subject_allowed(
            repo, subject, component=component, release_suffix=release_suffix
        )
        for subject in subject_lines
    )


def _cleanup_followup_allowed(
    repo: RepoConfig,
    *,
    subject_lines: list[str],
    paths: list[str],
    name_status_lines: list[str],
) -> bool:
    if not paths or not subject_lines or not name_status_lines:
        return False
    if not all(_RELEASE_CLEANUP_SUBJECT.match(subject) for subject in subject_lines):
        return False
    patterns = set(repo.list_value("non_release_paths"))
    patterns.update(RETIRED_SHARED_PATHS)
    if not patterns or not all(
        _matches_release_pattern(path, patterns) for path in paths
    ):
        return False
    return all(
        _cleanup_status_line_allowed(line, patterns) for line in name_status_lines
    )


def _cleanup_status_line_allowed(line: str, patterns: set[str]) -> bool:
    parts = line.split("\t")
    if len(parts) < 2:
        return False
    status = parts[0]
    if status != "D":
        return False
    path = parts[1].strip()
    return bool(path) and _matches_release_pattern(path, patterns)


def _non_publish_patterns(repo: RepoConfig) -> set[str]:
    patterns = set(repo.list_value("non_release_paths"))
    patterns.update(RETIRED_SHARED_PATHS)
    return patterns


def _sha_tag_skip_patterns(repo: RepoConfig) -> set[str]:
    patterns = _non_publish_patterns(repo)
    return {pattern for pattern in patterns if pattern}


def _sha_tag_skip_paths_only(repo: RepoConfig, paths: list[str]) -> bool:
    patterns = _sha_tag_skip_patterns(repo)
    return (
        bool(paths)
        and bool(patterns)
        and all(_matches_release_pattern(path, patterns) for path in paths)
    )


def _component_change_unrelated(
    repo: RepoConfig, component: str, paths: list[str]
) -> bool:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return False
    relevant_paths = [
        path
        for path in paths
        if not _matches_release_pattern(path, _sha_tag_skip_patterns(repo))
    ]
    if not relevant_paths:
        return True
    patterns = _component_specific_publish_patterns(repo, component)
    if not patterns:
        return False
    return not any(_matches_release_pattern(path, patterns) for path in relevant_paths)


def _component_specific_publish_patterns(repo: RepoConfig, component: str) -> set[str]:
    components = repo.raw.get("components")
    raw_config: dict[str, object] = {}
    if isinstance(components, dict):
        candidate = components.get(component)
        if isinstance(candidate, dict):
            raw_config = candidate

    config = component_config(repo, component)
    patterns: set[str] = set()
    include_defaults = component == "aio"
    for key, default in (
        ("dockerfile", "Dockerfile"),
        ("upstream_config", "upstream.toml"),
        ("release_changelog", "CHANGELOG.md"),
    ):
        if include_defaults or key in raw_config:
            value = str(config.get(key, default)).strip()
            if value:
                patterns.add(value)

    if include_defaults:
        patterns.update({"rootfs/**"})
        if not isinstance(components, dict):
            patterns.update(repo.list_value("xml_paths"))

    context = str(raw_config.get("context", "") or "").strip()
    if context:
        patterns.add(context)
        patterns.add(f"{context.rstrip('/')}/**")

    patterns.update(_string_list(config.get("xml_paths", [])))
    patterns.update(_string_list(config.get("publish_paths", [])))
    for monitor in repo.raw.get("upstream_monitor", []):
        if (
            isinstance(monitor, dict)
            and str(monitor.get("component", "aio")) == component
            and monitor.get("dockerfile")
        ):
            patterns.add(str(monitor["dockerfile"]))
    return {pattern for pattern in patterns if pattern}


def _string_list(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value.strip() else set()
    if isinstance(value, list):
        return {str(item) for item in value if str(item).strip()}
    return set()


def _matches_release_pattern(path: str, patterns: set[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if path == normalized or path.startswith(f"{normalized}/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _component_release_followup_paths(repo: RepoConfig, component: str) -> set[str]:
    paths = {"CHANGELOG.md"}
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return paths
    for name, config in components.items():
        if name == component or not isinstance(config, dict):
            continue
        xml_paths = config.get("xml_paths", [])
        if isinstance(xml_paths, str):
            candidate_paths = [xml_paths]
        elif isinstance(xml_paths, list):
            candidate_paths = [str(path) for path in xml_paths]
        else:
            candidate_paths = []
        paths.update(path for path in candidate_paths if path.endswith(".xml"))
    return paths


def _changed_paths_between(repo_path: Path, base: str, head: str) -> list[str]:
    return [
        path.strip()
        for path in git(
            repo_path, "diff", "--name-only", f"{base}..{head}"
        ).splitlines()
        if path.strip()
    ]


def _release_followup_subject_allowed(
    repo: RepoConfig, subject: str, *, component: str, release_suffix: str
) -> bool:
    if _RELEASE_FORMAT_SUBJECT.match(subject):
        return True
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return False
    for name, config in components.items():
        if name == component or not isinstance(config, dict):
            continue
        other_suffix = str(config.get("release_suffix", "aio"))
        if other_suffix == release_suffix:
            continue
        pattern = re.compile(
            rf"^chore\(release\): .+-{re.escape(other_suffix)}\.\d+" r"(?: \(#\d+\))?$"
        )
        if pattern.match(subject):
            return True
    return False
