from __future__ import annotations

import subprocess  # nosec B404
from argparse import Namespace
from pathlib import Path

from aio_fleet.catalog_changelog import render_catalog_changelog
from aio_fleet.cli import cmd_catalog_changelog


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # nosec


def _commit(repo: Path, message: str) -> None:
    marker = repo / f"{len(list(repo.iterdir()))}.txt"
    marker.write_text(message)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "config", "commit.gpgsign", "false")


def test_catalog_changelog_matches_catalog_grouping(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "ci(sync): publish catalog metadata (#123)")
    _commit(tmp_path, "chore(changelog): update catalog history")
    _commit(tmp_path, "docs(readme): refresh catalog list")
    _commit(tmp_path, "fix(template): normalize icon")
    _commit(tmp_path, "chore(deps): update create-pull-request")

    changelog = render_catalog_changelog(tmp_path)

    assert "### CI\n\n- Publish catalog metadata" in changelog  # nosec B101
    assert "### Documentation\n\n- Refresh catalog list" in changelog  # nosec B101
    assert "### Fixes\n\n- Normalize icon" in changelog  # nosec B101
    assert (
        "### Dependency Updates\n\n- Update create-pull-request" in changelog
    )  # nosec B101
    assert "update catalog history" not in changelog  # nosec B101
    assert "(#123)" not in changelog  # nosec B101


def test_catalog_changelog_check_reports_drift(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "ci(sync): publish catalog metadata")
    (tmp_path / "CHANGELOG.md").write_text("# stale\n")
    args = Namespace(catalog_path=str(tmp_path), write=False, check=True)

    assert cmd_catalog_changelog(args) == 1  # nosec B101

    args.write = True
    assert cmd_catalog_changelog(args) == 0  # nosec B101
    args.write = False
    assert cmd_catalog_changelog(args) == 0  # nosec B101
