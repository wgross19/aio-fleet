from __future__ import annotations

from pathlib import Path

from aio_fleet.catalog import sync_catalog_assets, unpublished_xml_targets
from aio_fleet.manifest import RepoConfig


class _Manifest:
    raw: dict[str, object] = {}


def _repo(path: Path, *, catalog_published: bool = True) -> RepoConfig:
    raw = {
        "path": str(path),
        "app_slug": "example-aio",
        "image_name": "wgross19/example-aio",
        "docker_cache_scope": "example-aio-image",
        "pytest_image_tag": "example-aio:pytest",
        "catalog_published": catalog_published,
        "catalog_assets": [
            {"source": "example-aio.xml", "target": "example-aio.xml"},
            {"source": "assets/icon.png", "target": "icons/example.png"},
        ],
    }
    return RepoConfig(name="example-aio", raw=raw, defaults={}, owner="wgross19")


def test_sync_catalog_skips_unpublished_xml_but_allows_icons(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / "assets").mkdir(parents=True)
    (repo_path / "example-aio.xml").write_text("<Container />\n")
    (repo_path / "assets" / "icon.png").write_bytes(b"icon")
    catalog_path = tmp_path / "catalog"

    repo = _repo(repo_path, catalog_published=False)
    changes = sync_catalog_assets(
        _Manifest(),  # type: ignore[arg-type]
        catalog_path=catalog_path,
        repos=[repo],
        icon_only=False,
        dry_run=False,
    )

    assert [
        change.target.relative_to(catalog_path).as_posix() for change in changes
    ] == [  # nosec B101
        "icons/example.png"
    ]
    assert not (catalog_path / "example-aio.xml").exists()  # nosec B101
    assert (
        catalog_path / "icons" / "example.png"
    ).read_bytes() == b"icon"  # nosec B101


def test_unpublished_xml_targets_reports_blocked_assets(tmp_path: Path) -> None:
    repo = _repo(tmp_path, catalog_published=False)

    assert unpublished_xml_targets(_Manifest(), [repo]) == [  # type: ignore[arg-type] # nosec B101
        "example-aio: example-aio.xml"
    ]


def test_sync_catalog_rejects_source_path_traversal(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = _repo(repo_path)
    repo.raw["catalog_assets"] = [
        {"source": "../secret.txt", "target": "icons/example.png"}
    ]

    try:
        sync_catalog_assets(
            _Manifest(),  # type: ignore[arg-type]
            catalog_path=tmp_path / "catalog",
            repos=[repo],
            icon_only=False,
            dry_run=True,
        )
    except ValueError as exc:
        assert "source path is invalid" in str(exc)  # nosec B101
    else:
        raise AssertionError("expected ValueError for source traversal")


def test_sync_catalog_rejects_target_path_traversal(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / "assets").mkdir(parents=True)
    (repo_path / "assets" / "icon.png").write_bytes(b"icon")
    repo = _repo(repo_path)
    repo.raw["catalog_assets"] = [
        {"source": "assets/icon.png", "target": "../escaped/icon.png"}
    ]

    try:
        sync_catalog_assets(
            _Manifest(),  # type: ignore[arg-type]
            catalog_path=tmp_path / "catalog",
            repos=[repo],
            icon_only=False,
            dry_run=False,
        )
    except ValueError as exc:
        assert "target path is invalid" in str(exc)  # nosec B101
    else:
        raise AssertionError("expected ValueError for target traversal")

    assert not (tmp_path / "escaped" / "icon.png").exists()  # nosec B101


def test_sync_catalog_rejects_reserved_git_target(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / "assets").mkdir(parents=True)
    (repo_path / "assets" / "hook").write_text("#!/bin/sh\n")
    repo = _repo(repo_path)
    repo.raw["catalog_assets"] = [
        {"source": "assets/hook", "target": ".git/hooks/pre-push"}
    ]

    try:
        sync_catalog_assets(
            _Manifest(),  # type: ignore[arg-type]
            catalog_path=tmp_path / "catalog",
            repos=[repo],
            icon_only=False,
            dry_run=False,
        )
    except ValueError as exc:
        assert "target path is reserved" in str(exc)  # nosec B101
    else:
        raise AssertionError("expected ValueError for reserved git target")
