from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """Raised when fleet.yml is invalid."""


@dataclass(frozen=True)
class RepoConfig:
    name: str
    raw: dict[str, Any]
    defaults: dict[str, Any]
    owner: str

    @property
    def path(self) -> Path:
        return Path(str(self.raw["path"]))

    @property
    def app_slug(self) -> str:
        return str(self.raw.get("app_slug", self.name))

    @property
    def workflow_name(self) -> str:
        return str(self.raw.get("workflow_name", f"CI / {self.app_slug}"))

    @property
    def image_name(self) -> str:
        return str(self.raw["image_name"])

    @property
    def github_repo(self) -> str:
        return str(self.raw.get("github_repo", f"{self.owner}/{self.name}"))

    @property
    def publish_profile(self) -> str:
        return str(self.raw.get("publish_profile", "changelog-version"))

    @property
    def is_signoz_suite(self) -> bool:
        return self.publish_profile == "signoz-suite"

    @property
    def is_multi_component(self) -> bool:
        return self.publish_profile in {"multi-component", "signoz-suite"}

    @property
    def extended_integration(self) -> dict[str, Any] | None:
        value = self.raw.get("extended_integration")
        return value if isinstance(value, dict) else None

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, self.defaults.get(key, default))

    def list_value(self, key: str) -> list[str]:
        value = self.raw.get(key, self.defaults.get(key, []))
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]


@dataclass(frozen=True)
class FleetManifest:
    path: Path
    raw: dict[str, Any]

    @property
    def owner(self) -> str:
        return str(self.raw["owner"])

    @property
    def defaults(self) -> dict[str, Any]:
        return dict(self.raw.get("defaults", {}))

    @property
    def reusable_workflow(self) -> dict[str, Any]:
        return dict(self.raw.get("reusable_workflow", {}))

    @property
    def repos(self) -> dict[str, RepoConfig]:
        repos = self.raw.get("repos", {})
        return {
            name: RepoConfig(
                name=name,
                raw=dict(config),
                defaults=self.defaults,
                owner=self.owner,
            )
            for name, config in repos.items()
        }

    def repo(self, name: str) -> RepoConfig:
        try:
            return self.repos[name]
        except KeyError as exc:
            raise ManifestError(f"unknown repo in fleet.yml: {name}") from exc


def load_manifest(path: Path = Path("fleet.yml")) -> FleetManifest:
    if not path.exists():
        raise ManifestError(f"manifest not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ManifestError("fleet.yml must contain a mapping")
    manifest = FleetManifest(path=path, raw=data)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: FleetManifest) -> None:
    required_top = ["owner", "repos"]
    for key in required_top:
        if key not in manifest.raw:
            raise ManifestError(f"fleet.yml missing required key: {key}")
    if not manifest.repos:
        raise ManifestError("fleet.yml must define at least one repo")

    for name, repo in manifest.repos.items():
        if "public" not in repo.raw:
            raise ManifestError(f"{name} missing required key: public")
        if not isinstance(repo.raw["public"], bool):
            raise ManifestError(f"{name} public must be true or false")
        for key in [
            "path",
            "app_slug",
            "image_name",
            "docker_cache_scope",
            "pytest_image_tag",
        ]:
            if key not in repo.raw:
                raise ManifestError(f"{name} missing required key: {key}")
        if repo.publish_profile not in {
            "template",
            "upstream-aio-track",
            "changelog-version",
            "dify",
            "multi-component",
            "signoz-suite",
        }:
            raise ManifestError(
                f"{name} has unsupported publish_profile: {repo.publish_profile}"
            )
        if repo.is_multi_component and "components" not in repo.raw:
            raise ManifestError(
                f"{name} {repo.publish_profile} profile requires components"
            )
        _validate_components(name, repo)


def _validate_components(name: str, repo: RepoConfig) -> None:
    components = repo.raw.get("components")
    if components is None:
        return
    if not isinstance(components, dict) or not components:
        raise ManifestError(f"{name} components must be a non-empty mapping")

    for component_name, config in components.items():
        if not isinstance(config, dict):
            raise ManifestError(f"{name} component {component_name} must be a mapping")

        release_policy = str(config.get("release_policy", "standard"))
        if release_policy not in {"standard", "registry_only"}:
            raise ManifestError(
                f"{name} component {component_name} has unsupported release_policy: {release_policy}"
            )

        if component_name != "aio" or release_policy == "registry_only":
            for key in ["image_name", "dockerfile"]:
                if key not in config:
                    raise ManifestError(
                        f"{name} component {component_name} missing required key: {key}"
                    )
