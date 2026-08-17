from __future__ import annotations

from pathlib import Path

from aio_fleet.cli import _catalog_status, _publish_status
from aio_fleet.manifest import RepoConfig


def _repo(tmp_path: Path, **overrides: object) -> RepoConfig:
    raw = {
        "path": str(tmp_path / "repo"),
        "app_slug": "example-aio",
        "image_name": "wgross19/example-aio",
        "docker_cache_scope": "example-aio-image",
        "pytest_image_tag": "example-aio:pytest",
        "publish_profile": "changelog-version",
        "catalog_assets": [
            {"source": "example-aio.xml", "target": "example-aio.xml"},
            {"source": "assets/app-icon.png", "target": "icons/example.png"},
        ],
    }
    raw.update(overrides)
    return RepoConfig(name="example-aio", raw=raw, defaults={}, owner="wgross19")


def test_catalog_status_reports_missing_catalog_assets(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog"
    catalog_path.mkdir()

    assert _catalog_status(_repo(tmp_path), catalog_path) == (  # nosec B101
        "catalog=missing:example-aio.xml,icons/example.png"
    )


def test_catalog_status_respects_unpublished_repos(tmp_path: Path) -> None:
    assert (
        _catalog_status(_repo(tmp_path, catalog_published=False), tmp_path)
        == "catalog=held"
    )  # nosec B101


def test_publish_status_blocks_dirty_or_drifted_repos(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert _publish_status(
        repo, dirty="dirty", behind="0", policy_state=" policy=ok"
    ) == (  # nosec B101
        "publish=blocked:dirty"
    )
    assert _publish_status(
        repo, dirty="clean", behind="1", policy_state=" policy=ok"
    ) == (  # nosec B101
        "publish=blocked:behind"
    )
    assert _publish_status(
        repo, dirty="clean", behind="0", policy_state=" policy=ok"
    ) == (  # nosec B101
        "publish=source-ready"
    )
