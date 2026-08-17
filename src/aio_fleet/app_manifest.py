from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aio_fleet.manifest import ManifestError, RepoConfig

APP_MANIFEST_NAME = ".aio-fleet.yml"


def app_manifest_from_repo(repo: RepoConfig) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "repo": repo.name,
        "github_repo": repo.github_repo,
        "app_slug": repo.app_slug,
        "image": {
            "name": repo.image_name,
            "cache_scope": repo.raw["docker_cache_scope"],
            "pytest_tag": repo.raw["pytest_image_tag"],
            "publish_platforms": repo.get("publish_platforms"),
        },
        "release": {
            "name": repo.get("release_name", repo.app_slug),
            "profile": repo.publish_profile,
            "previous_tag_command": repo.get("previous_tag_command", "latest-aio-tag"),
        },
        "upstream": {
            "name": repo.get("upstream_name", repo.app_slug),
            "version_key": repo.get("upstream_version_key", "UPSTREAM_VERSION"),
            "digest_arg": repo.get("upstream_digest_arg", "UPSTREAM_IMAGE_DIGEST"),
            "commit_paths": repo.list_value("upstream_commit_paths"),
            "components": repo.raw.get("upstream_components", []),
            "monitor": repo.raw.get("upstream_monitor", []),
        },
        "template": {
            "xml_paths": repo.list_value("xml_paths"),
            "generated": bool(repo.raw.get("generated_template", False)),
            "generator_check_command": repo.get("generator_check_command", ""),
        },
        "catalog": {
            "published": repo.raw.get("catalog_published", True),
            "assets": repo.raw.get("catalog_assets", []),
        },
        "tests": {
            "unit": repo.get("unit_pytest_args"),
            "integration": repo.get("integration_pytest_args"),
            "extended_integration": repo.get("extended_integration", None),
            "checkout_submodules": bool(repo.get("checkout_submodules", False)),
        },
        "validation": repo.raw.get("validation", {}),
        "runtime_contract": repo.raw.get("runtime_contract", {}),
        "components": repo.raw.get("components", {}),
    }
    return _drop_empty(manifest)


def render_app_manifest(repo: RepoConfig) -> str:
    return yaml.dump(
        app_manifest_from_repo(repo),
        Dumper=_IndentedSafeDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
        width=4096,
    )


def load_app_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"app manifest not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ManifestError(f"{APP_MANIFEST_NAME} must contain a mapping")
    validate_app_manifest(data)
    return data


def validate_app_manifest(data: dict[str, Any]) -> None:
    required = ["schema_version", "repo", "github_repo", "app_slug", "image", "release"]
    for key in required:
        if key not in data:
            raise ManifestError(f"{APP_MANIFEST_NAME} missing required key: {key}")
    if data["schema_version"] != 1:
        raise ManifestError(f"unsupported {APP_MANIFEST_NAME} schema_version")
    for section in ["image", "release"]:
        if not isinstance(data.get(section), dict):
            raise ManifestError(f"{APP_MANIFEST_NAME} {section} must be a mapping")
    image = data["image"]
    for key in ["name", "cache_scope", "pytest_tag"]:
        if key not in image:
            raise ManifestError(
                f"{APP_MANIFEST_NAME} image missing required key: {key}"
            )
    release = data["release"]
    for key in ["name", "profile"]:
        if key not in release:
            raise ManifestError(
                f"{APP_MANIFEST_NAME} release missing required key: {key}"
            )


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := _drop_empty(item)) not in ({}, [], None, "")
        }
    if isinstance(value, list):
        return [_drop_empty(item) for item in value if _drop_empty(item) is not None]
    return value


class _IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:  # type: ignore[override]
        return super().increase_indent(flow, indentless=False)


def _represent_string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = (
        '"' if value.endswith(":") or value.isdigit() or value.startswith("*") else None
    )
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_IndentedSafeDumper.add_representer(str, _represent_string)
