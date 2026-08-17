from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from aio_fleet.cleanup import cleanup_findings
from aio_fleet.cli import cmd_cleanup_repo
from aio_fleet.manifest import load_manifest


def test_cleanup_findings_detect_retired_shared_files(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".github" / "workflows").mkdir(parents=True)
    (repo_path / ".github" / "dependabot.yml").write_text("version: 2\n")
    (repo_path / ".trunk").mkdir()
    (repo_path / "renovate.json").write_text("{}\n")
    (repo_path / "requirements-dev.txt").write_text("pytest\n")
    (repo_path / "scripts").mkdir()
    (repo_path / "scripts" / "release.py").write_text("")
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
    repo = load_manifest(manifest).repo("example-aio")

    findings = cleanup_findings(repo)

    assert [
        finding.path.relative_to(repo_path).as_posix() for finding in findings
    ] == [  # nosec B101
        ".github/workflows",
        ".github/dependabot.yml",
        ".trunk",
        "renovate.json",
        "requirements-dev.txt",
        "scripts/release.py",
    ]


def test_cleanup_findings_allow_manifest_owned_upstream_config(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "upstream.toml").write_text("[upstream]\n")
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
    components:
      aio:
        upstream_config: upstream.toml
""")
    repo = load_manifest(manifest).repo("example-aio")

    assert cleanup_findings(repo) == []  # nosec B101


def test_cleanup_fix_removes_retired_shared_files(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    workflows = repo_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "legacy.yml").write_text("name: legacy\n")
    (repo_path / "scripts").mkdir()
    (repo_path / "scripts" / "release.py").write_text("")
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
    args = Namespace(
        manifest=str(manifest),
        all=False,
        repo="example-aio",
        repo_path=None,
        verify=True,
        remove=False,
        fix=True,
        dry_run=False,
        format="json",
    )

    assert cmd_cleanup_repo(args) == 0  # nosec B101
    assert not workflows.exists()  # nosec B101
    assert not (repo_path / "scripts" / "release.py").exists()  # nosec B101
