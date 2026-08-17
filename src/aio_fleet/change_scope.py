from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from aio_fleet.cleanup import RETIRED_SHARED_PATHS
from aio_fleet.manifest import RepoConfig

CHECK_MODE_FULL = "full"
CHECK_MODE_FAST_CLEANUP = "fast-cleanup"


@dataclass(frozen=True)
class ChangeScope:
    check_mode: str
    changed_paths: tuple[str, ...] = ()
    fast_path_reason: str = ""


def classify_required_check_scope(
    repo: RepoConfig,
    changed_paths: Sequence[str] | None,
    *,
    changed_file_statuses: Mapping[str, str] | None = None,
    publish: bool = False,
    fast_path_disabled: bool = False,
) -> ChangeScope:
    normalized = normalize_changed_paths(changed_paths)
    statuses = normalize_changed_file_statuses(changed_file_statuses)
    if fast_path_disabled:
        return ChangeScope(
            CHECK_MODE_FULL,
            normalized,
            "fast path disabled",
        )
    if publish:
        return ChangeScope(
            CHECK_MODE_FULL,
            normalized,
            "publish requested",
        )
    if not normalized:
        return ChangeScope(
            CHECK_MODE_FULL,
            normalized,
            "changed paths unresolved",
        )

    for path in normalized:
        if _publish_or_catalog_path(repo, path):
            return ChangeScope(
                CHECK_MODE_FULL,
                normalized,
                f"publish/catalog path: {path}",
            )
        if _safe_cleanup_path(repo, path, status=statuses.get(path, "")):
            continue
        if _requires_full_path(path):
            return ChangeScope(
                CHECK_MODE_FULL,
                normalized,
                f"required validation path: {path}",
            )
        return ChangeScope(
            CHECK_MODE_FULL,
            normalized,
            f"unclassified path: {path}",
        )

    return ChangeScope(
        CHECK_MODE_FAST_CLEANUP,
        normalized,
        "cleanup/local-hygiene-only paths",
    )


def normalize_changed_paths(changed_paths: Sequence[str] | None) -> tuple[str, ...]:
    if changed_paths is None:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in changed_paths:
        path = str(raw_path).strip().replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        path = path.rstrip("/")
        if not path or path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return tuple(normalized)


def normalize_changed_file_statuses(
    changed_file_statuses: Mapping[str, str] | None,
) -> dict[str, str]:
    if not changed_file_statuses:
        return {}
    normalized: dict[str, str] = {}
    for raw_path, raw_status in changed_file_statuses.items():
        paths = normalize_changed_paths([raw_path])
        if not paths:
            continue
        normalized[paths[0]] = str(raw_status).strip().lower()
    return normalized


def publish_components_for_changed_paths(
    repo: RepoConfig, changed_paths: Sequence[str]
) -> list[str]:
    paths = normalize_changed_paths(changed_paths)
    return [
        component
        for component in publish_component_names(repo)
        if any(
            path_matches_patterns(
                path,
                publish_related_patterns_for_component(repo, component),
            )
            for path in paths
        )
    ]


def publish_related_patterns(repo: RepoConfig) -> set[str]:
    patterns: set[str] = set()
    for component in publish_component_names(repo):
        patterns.update(publish_related_patterns_for_component(repo, component))
    return {pattern for pattern in patterns if pattern}


def publish_related_patterns_for_component(
    repo: RepoConfig, component: str
) -> set[str]:
    config = _component_config(repo, component)
    patterns = {".aio-fleet.yml", "CHANGELOG.md"}
    if component == "aio" or not config:
        patterns.update({"Containerfile", "Dockerfile", "rootfs/**", "upstream.toml"})
        for key in ("extra_publish_paths", "upstream_commit_paths"):
            patterns.update(repo.list_value(key))
        if not config:
            patterns.update(repo.list_value("xml_paths"))

    if config:
        for key in ("dockerfile", "upstream_config"):
            value = str(config.get(key, "")).strip()
            if value:
                patterns.add(value)
        patterns.update(_string_list(config.get("xml_paths", [])))
        patterns.update(_string_list(config.get("publish_paths", [])))
        release_changelog = str(config.get("release_changelog", "") or "").strip()
        if release_changelog:
            patterns.add(release_changelog)

    for monitor in repo.raw.get("upstream_monitor", []):
        if (
            isinstance(monitor, dict)
            and str(monitor.get("component", "aio")) == component
            and monitor.get("dockerfile")
        ):
            patterns.add(str(monitor["dockerfile"]))
    return {pattern for pattern in patterns if pattern}


def publish_component_names(repo: RepoConfig) -> list[str]:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return ["aio"]
    names = [
        name
        for name, config in components.items()
        if name == "aio" or (isinstance(config, dict) and config.get("image_name"))
    ]
    return names or ["aio"]


def path_matches_patterns(path: str, patterns: set[str] | Sequence[str]) -> bool:
    for raw_pattern in patterns:
        pattern = str(raw_pattern).strip().replace("\\", "/")
        if not pattern:
            continue
        exact_pattern = pattern.rstrip("/")
        if path == exact_pattern or path.startswith(f"{exact_pattern}/"):
            return True
        if fnmatch(path, pattern):
            return True
    return False


def _publish_or_catalog_path(repo: RepoConfig, path: str) -> bool:
    if (
        path == "upstream.toml"
        and not _retired_upstream_config_is_manifest_owned(repo)
        and not _explicit_repo_publish_path(repo, path)
    ):
        return False
    if path_matches_patterns(path, publish_related_patterns(repo)):
        return True
    if _xml_path(path):
        return True
    if _catalog_asset_path(repo, path):
        return True
    if path_matches_patterns(
        path,
        {
            "assets/**",
            "icons/**",
            "screenshots/**",
            "templates/**",
            "Containerfile",
            "Dockerfile",
            "docker/**",
            "rootfs/**",
        },
    ):
        return True
    return False


def _catalog_asset_path(repo: RepoConfig, path: str) -> bool:
    patterns: set[str] = set()
    for asset in repo.raw.get("catalog_assets", []):
        if isinstance(asset, dict):
            source = str(asset.get("source", "")).strip()
            if source:
                patterns.add(source)
    return path_matches_patterns(path, patterns)


def _explicit_repo_publish_path(repo: RepoConfig, path: str) -> bool:
    patterns: set[str] = set()
    for key in ("extra_publish_paths", "upstream_commit_paths", "xml_paths"):
        patterns.update(repo.list_value(key))
    return path_matches_patterns(path, patterns)


def _safe_cleanup_path(repo: RepoConfig, path: str, *, status: str) -> bool:
    if path_matches_patterns(
        path,
        {
            ".trunk",
            ".trunk/**",
            "AGENTS.md",
            "SECURITY.md",
            "docs/**",
        },
    ):
        return True
    if path_matches_patterns(path, _retired_cleanup_patterns(repo)):
        return status in {"removed", "deleted"}
    return False


def _retired_cleanup_patterns(repo: RepoConfig) -> set[str]:
    patterns: set[str] = set()
    for relative in RETIRED_SHARED_PATHS:
        if relative == ".github/workflows":
            continue
        if relative == "upstream.toml" and _retired_upstream_config_is_manifest_owned(
            repo
        ):
            continue
        patterns.add(relative)
        patterns.add(f"{relative}/**")
    return patterns


def _retired_upstream_config_is_manifest_owned(repo: RepoConfig) -> bool:
    expected = Path("upstream.toml")
    candidates: list[object] = [repo.get("upstream_config")]
    components = repo.get("components", {})
    if isinstance(components, dict):
        for component in components.values():
            if isinstance(component, dict):
                candidates.append(component.get("upstream_config"))
    return any(
        Path(str(candidate)) == expected for candidate in candidates if candidate
    )


def _requires_full_path(path: str) -> bool:
    if path in {"fleet.yml", "pyproject.toml", ".aio-fleet.yml"}:
        return True
    name = Path(path).name
    if name.startswith("CHANGELOG") and name.endswith(".md"):
        return True
    return path_matches_patterns(
        path,
        {
            ".github/**",
            "scripts/**",
            "src/**",
            "tests/**",
            "test/**",
            "tools/**",
            "bin/**",
            "components/**",
            "patches/**",
            "config/**",
        },
    )


def _xml_path(path: str) -> bool:
    return path.endswith(".xml") or path_matches_patterns(path, {"**/*.xml"})


def _component_config(repo: RepoConfig, component: str) -> dict[str, object]:
    components = repo.raw.get("components")
    if not isinstance(components, dict):
        return {}
    config = components.get(component)
    return config if isinstance(config, dict) else {}


def _string_list(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value if str(item).strip()}
    return set()
