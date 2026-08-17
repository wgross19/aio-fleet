from __future__ import annotations

import shutil
import subprocess  # nosec B404
from pathlib import Path

import pytest

from aio_fleet.changelog import (
    ReleasePlan,
    build_release_plan,
    normalize_markdown_changelog,
    render_git_cliff_config,
    update_registry_revision_arg,
    update_template_changes,
)
from aio_fleet.manifest import RepoConfig


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # nosec


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "config", "commit.gpgsign", "false")


def test_component_release_plan_uses_component_xml_and_suffix(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "components" / "agent").mkdir(parents=True)
    (tmp_path / "Dockerfile").write_text("ARG UPSTREAM_SIGNOZ_VERSION=v0.121.1\n")
    (tmp_path / "components" / "agent" / "Dockerfile").write_text(
        "ARG UPSTREAM_OTELCOL_VERSION=0.151.0\n"
    )
    (tmp_path / "signoz-aio.xml").write_text(
        "<Container><Changes>old</Changes></Container>"
    )
    (tmp_path / "signoz-agent.xml").write_text(
        "<Container><Changes>old</Changes></Container>"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feat(test): initial")

    repo = RepoConfig(
        name="signoz-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "signoz-aio",
            "image_name": "wgross19/signoz-aio",
            "docker_cache_scope": "signoz-aio-image",
            "pytest_image_tag": "signoz-aio:pytest",
            "publish_profile": "signoz-suite",
            "upstream_version_key": "UPSTREAM_SIGNOZ_VERSION",
            "catalog_assets": [
                {"source": "signoz-aio.xml", "target": "signoz-aio.xml"},
                {"source": "signoz-agent.xml", "target": "signoz-agent.xml"},
            ],
            "components": {
                "aio": {
                    "dockerfile": "Dockerfile",
                    "xml_paths": ["signoz-aio.xml"],
                    "upstream_version_key": "UPSTREAM_SIGNOZ_VERSION",
                    "release_suffix": "aio",
                },
                "agent": {
                    "dockerfile": "components/agent/Dockerfile",
                    "xml_paths": ["signoz-agent.xml"],
                    "upstream_version_key": "UPSTREAM_OTELCOL_VERSION",
                    "release_suffix": "agent",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )

    aio = build_release_plan(repo, component="aio")
    agent = build_release_plan(repo, component="agent")

    assert aio.version == "v0.121.1-aio.1"  # nosec B101
    assert aio.xml_paths == [tmp_path / "signoz-aio.xml"]  # nosec B101
    assert ".*-aio\\.[0-9]+" in aio.cliff_config  # nosec B101
    assert agent.version == "0.151.0-agent.1"  # nosec B101
    assert agent.xml_paths == [tmp_path / "signoz-agent.xml"]  # nosec B101
    assert ".*-agent\\.[0-9]+" in agent.cliff_config  # nosec B101


def test_component_release_plan_uses_component_changelog(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile.alpha").write_text("ARG UPSTREAM_VERSION=0.7.1-alpha.7\n")
    (tmp_path / "upstream.toml").write_text("")
    (tmp_path / "CHANGELOG.md").write_text("# Stable\n")
    (tmp_path / "CHANGELOG.alpha.md").write_text("# Alpha\n")
    (tmp_path / "sure-aio-alpha.xml").write_text(
        "<Container><Changes>old</Changes></Container>"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feat(test): initial")

    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
            "components": {
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_suffix": "aio",
                    "xml_paths": ["sure-aio-alpha.xml"],
                }
            },
        },
        defaults={},
        owner="wgross19",
    )

    alpha = build_release_plan(repo, component="sure-alpha")

    assert alpha.changelog_path == tmp_path / "CHANGELOG.alpha.md"  # nosec B101


def test_component_release_plan_uses_prefixed_component_tags(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile.alpha").write_text("ARG UPSTREAM_VERSION=0.7.1-alpha.9\n")
    (tmp_path / "upstream.toml").write_text("")
    (tmp_path / "CHANGELOG.alpha.md").write_text("# Alpha\n")
    (tmp_path / "sure-aio-alpha.xml").write_text(
        "<Container><Changes>old</Changes></Container>"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feat(test): initial")
    _git(tmp_path, "tag", "sure-alpha/0.7.1-alpha.9-aio.1")

    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
            "components": {
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                    "xml_paths": ["sure-aio-alpha.xml"],
                }
            },
        },
        defaults={},
        owner="wgross19",
    )

    alpha = build_release_plan(repo, component="sure-alpha")

    assert alpha.version == "0.7.1-alpha.9-aio.2"  # nosec B101
    assert alpha.release_tag_prefix == "sure-alpha/"  # nosec B101
    assert "^sure\\-alpha/v?[0-9].*-aio\\.[0-9]+" in alpha.cliff_config  # nosec B101


def test_registry_revision_update_rejects_duplicate_args(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.alpha"
    dockerfile.write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.11\n"
        "ARG AIO_REVISION=1\n"
        "ARG AIO_REVISION=1\n"
    )
    plan = ReleasePlan(
        version="0.7.1-alpha.11-aio.2",
        changelog_path=tmp_path / "CHANGELOG.alpha.md",
        xml_paths=[],
        cliff_config="",
        registry_revision_path=dockerfile,
        registry_revision_arg="AIO_REVISION",
        registry_revision_value="2",
    )

    with pytest.raises(ValueError, match="Expected exactly one ARG AIO_REVISION"):
        update_registry_revision_arg(plan)

    assert dockerfile.read_text().count("ARG AIO_REVISION=1") == 2  # nosec B101


def test_component_xml_changes_note_uses_component_changelog(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.alpha.md"
    template = tmp_path / "sure-aio-alpha.xml"
    changelog.write_text(
        "# Alpha\n\n"
        "## 0.7.1-alpha.7-aio.6 - 2026-05-18\n\n"
        "### Alpha Customizations\n\n"
        "- Add strict alpha import preflight.\n"
    )
    template.write_text("<Container><Changes>old</Changes></Container>\n")

    update_template_changes(
        version="0.7.1-alpha.7-aio.6",
        changelog=changelog,
        template=template,
    )

    text = template.read_text()
    assert "Generated from CHANGELOG.alpha.md" in text  # nosec B101
    assert "Generated from CHANGELOG.md" not in text  # nosec B101


def test_git_cliff_config_renders_prettier_spaced_release_sections(
    tmp_path: Path,
) -> None:
    if shutil.which("git-cliff") is None:
        pytest.skip("git-cliff is not installed")

    _init_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "chore(test): initial")
    _git(tmp_path, "tag", "1.0.0-aio.1")
    (tmp_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "chore(sync): bump app to 1.0.1")

    repo = RepoConfig(
        name="example-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "example-aio",
            "image_name": "wgross19/example-aio",
            "docker_cache_scope": "example-aio-image",
            "pytest_image_tag": "example-aio:pytest",
            "publish_profile": "changelog-version",
            "upstream_version_key": "UPSTREAM_VERSION",
        },
        defaults={},
        owner="wgross19",
    )
    config = tmp_path / "cliff.toml"
    config.write_text(render_git_cliff_config(repo))

    result = subprocess.run(  # nosec B603
        [
            "git",
            "cliff",
            "--config",
            str(config),
            "--tag",
            "1.0.1-aio.1",
            "--unreleased",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(result.stdout)

    normalize_markdown_changelog(changelog)

    text = changelog.read_text()
    assert "## 1.0.1-aio.1 - " in text  # nosec B101
    assert "\n\n### Maintenance\n\n- Bump app to 1.0.1\n" in text  # nosec B101
    assert "\n\n\n" not in text  # nosec B101
