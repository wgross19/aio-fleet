from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aio_fleet.manifest import FleetManifest, RepoConfig


@dataclass(frozen=True)
class CatalogSyncChange:
    repo: str
    source: Path
    target: Path
    action: str


def sync_catalog_assets(
    manifest: FleetManifest,
    *,
    catalog_path: Path,
    repos: list[RepoConfig],
    icon_only: bool,
    dry_run: bool,
) -> list[CatalogSyncChange]:
    changes: list[CatalogSyncChange] = []
    for repo in repos:
        for source_rel, target_rel in _catalog_assets(repo):
            is_xml = target_rel.endswith(".xml")
            if icon_only and is_xml:
                continue
            if is_xml and repo.raw.get("catalog_published") is False:
                continue

            source = resolve_catalog_asset_path(
                repo.path,
                source_rel,
                repo_name=repo.name,
                field_name="source",
            )
            target = resolve_catalog_asset_path(
                catalog_path,
                target_rel,
                repo_name=repo.name,
                field_name="target",
            )
            if not source.exists():
                raise FileNotFoundError(
                    f"{repo.name}: catalog source missing: {source_rel}"
                )

            current = target.read_bytes() if target.exists() else None
            desired = source.read_bytes()
            if current == desired:
                continue

            action = "create" if current is None else "update"
            changes.append(
                CatalogSyncChange(
                    repo=repo.name, source=source, target=target, action=action
                )
            )
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    return changes


def unpublished_xml_targets(
    manifest: FleetManifest, repos: list[RepoConfig]
) -> list[str]:
    targets: list[str] = []
    for repo in repos:
        if repo.raw.get("catalog_published") is not False:
            continue
        for _source, target in _catalog_assets(repo):
            if target.endswith(".xml"):
                targets.append(f"{repo.name}: {target}")
    return targets


def _catalog_assets(repo: RepoConfig) -> list[tuple[str, str]]:
    assets = repo.raw.get("catalog_assets", [])
    if not isinstance(assets, list):
        raise ValueError(f"{repo.name}: catalog_assets must be a list")

    pairs: list[tuple[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError(f"{repo.name}: catalog_assets entries must be mappings")
        source = str(asset.get("source", "")).strip()
        target = str(asset.get("target", "")).strip()
        if not source or not target:
            raise ValueError(
                f"{repo.name}: catalog_assets entries require source and target"
            )
        pairs.append(
            (
                validate_catalog_asset_path(
                    source,
                    repo_name=repo.name,
                    field_name="source",
                ),
                validate_catalog_asset_path(
                    target,
                    repo_name=repo.name,
                    field_name="target",
                ),
            )
        )
    return pairs


def validate_catalog_asset_path(
    raw_path: str, *, repo_name: str, field_name: str
) -> str:
    normalized = raw_path.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = path.parts
    if (
        not normalized
        or normalized == "."
        or normalized.startswith("/")
        or normalized.startswith("./")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(
            f"{repo_name}: catalog_assets {field_name} path is invalid: {raw_path}"
        )
    if any(part == ".git" for part in parts):
        raise ValueError(
            f"{repo_name}: catalog_assets {field_name} path is reserved: {raw_path}"
        )
    return path.as_posix()


def resolve_catalog_asset_path(
    base: Path, raw_path: str, *, repo_name: str, field_name: str
) -> Path:
    safe_path = validate_catalog_asset_path(
        raw_path, repo_name=repo_name, field_name=field_name
    )
    base_path = base.resolve()
    resolved = (base_path / safe_path).resolve()
    try:
        resolved.relative_to(base_path)
    except ValueError as exc:
        raise ValueError(
            f"{repo_name}: catalog_assets {field_name} path escapes root: {raw_path}"
        ) from exc
    return resolved
