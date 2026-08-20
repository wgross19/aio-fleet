from __future__ import annotations

from pathlib import Path

import pytest

from aio_fleet.change_scope import (
    CHECK_MODE_FAST_CLEANUP,
    CHECK_MODE_FULL,
    classify_required_check_scope,
)
from aio_fleet.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fleet.yml"


def test_cleanup_scope_allows_retired_shared_and_local_hygiene_paths(
    tmp_path: Path,
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(
        repo,
        [
            ".github/ISSUE_TEMPLATE/bug.yml",
            ".trunk/trunk.yaml",
            "SECURITY.md",
            "AGENTS.md",
            "scripts/release.py",
            "upstream.toml",
            "tests/template/test_validate_template.py",
        ],
        changed_file_statuses={
            ".github/ISSUE_TEMPLATE/bug.yml": "removed",
            "scripts/release.py": "removed",
            "upstream.toml": "removed",
            "tests/template/test_validate_template.py": "removed",
        },
    )

    assert scope.check_mode == CHECK_MODE_FAST_CLEANUP  # nosec B101
    assert scope.fast_path_reason == "cleanup/local-hygiene-only paths"  # nosec B101


@pytest.mark.parametrize(
    "path",
    [
        ".aio-fleet.yml",
        ".github/workflows/old-ci.yml",
        "fleet.yml",
        "Dockerfile",
        "rootfs/etc/services.d/web/run",
        "example-aio.xml",
        "assets/icon.png",
        "icons/example.png",
        "screenshots/example/01.png",
        "CHANGELOG.md",
        "pyproject.toml",
        "tests/integration/test_smoke.py",
        "src/app.py",
        "unknown/path.txt",
    ],
)
def test_cleanup_scope_keeps_publish_runtime_catalog_and_unknown_paths_full(
    tmp_path: Path, path: str
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(repo, [path])

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
    assert path in scope.fast_path_reason  # nosec B101


def test_cleanup_scope_respects_component_publish_path_overrides() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    for path in ("README.md", "pyproject.toml", "docs/releases.md"):
        scope = classify_required_check_scope(repo, [path])

        assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
        assert "publish/catalog path" in scope.fast_path_reason  # nosec B101


def test_cleanup_scope_allows_docs_not_declared_as_publish_paths(
    tmp_path: Path,
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(repo, ["docs/support.md"])

    assert scope.check_mode == CHECK_MODE_FAST_CLEANUP  # nosec B101


def test_cleanup_scope_keeps_renamed_required_source_path_full(
    tmp_path: Path,
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(
        repo,
        ["docs/Dockerfile", "Dockerfile"],
        changed_file_statuses={
            "docs/Dockerfile": "renamed",
            "Dockerfile": "renamed-from",
        },
    )

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
    assert "Dockerfile" in scope.fast_path_reason  # nosec B101


def test_cleanup_scope_allows_docs_only_renames(tmp_path: Path) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(
        repo,
        ["docs/new.md", "docs/old.md"],
        changed_file_statuses={
            "docs/new.md": "renamed",
            "docs/old.md": "renamed-from",
        },
    )

    assert scope.check_mode == CHECK_MODE_FAST_CLEANUP  # nosec B101


@pytest.mark.parametrize(
    "path",
    [
        ".github/ISSUE_TEMPLATE/bug.yml",
        "scripts/release.py",
        "tests/template/test_validate_template.py",
    ],
)
def test_cleanup_scope_requires_retired_paths_to_be_deleted(
    tmp_path: Path, path: str
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(
        repo,
        [path],
        changed_file_statuses={path: "modified"},
    )

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101


@pytest.mark.parametrize("changed_paths", [None, []])
def test_cleanup_scope_fails_closed_when_paths_are_unresolved(
    tmp_path: Path, changed_paths: list[str] | None
) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(repo, changed_paths)

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
    assert scope.fast_path_reason == "changed paths unresolved"  # nosec B101


def test_cleanup_scope_never_fast_paths_publish_targets(tmp_path: Path) -> None:
    repo = load_manifest(_write_manifest(tmp_path)).repo("example-aio")

    scope = classify_required_check_scope(
        repo,
        [".trunk/trunk.yaml"],
        publish=True,
    )

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
    assert scope.fast_path_reason == "publish requested"  # nosec B101


def test_cleanup_scope_keeps_manifest_owned_upstream_config_full() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    scope = classify_required_check_scope(repo, ["upstream.toml"])

    assert scope.check_mode == CHECK_MODE_FULL  # nosec B101
    assert "publish/catalog path" in scope.fast_path_reason  # nosec B101


def _write_manifest(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    return manifest
