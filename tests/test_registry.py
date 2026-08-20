from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from aio_fleet import registry
from aio_fleet.control_plane import registry_publish_command
from aio_fleet.manifest import RepoConfig, load_manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fleet.yml"


@pytest.fixture(autouse=True)
def _clear_registry_verify_cache() -> None:
    registry._REGISTRY_TAG_SUCCESS_CACHE.clear()


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True)  # nosec B603 B607


def test_compute_registry_tags_preserves_docker_hub_and_ghcr_tags(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry, "_release_package_tag", lambda *_args, **_kwargs: "0.7.0-aio.1"
    )

    tags = registry.compute_registry_tags(repo, sha="a" * 40)

    assert tags.dockerhub == [  # nosec B101
        "wgross19/sure-aio:latest",
        "wgross19/sure-aio:0.7.0",
        "wgross19/sure-aio:0.7.0-aio.1",
        f"wgross19/sure-aio:sha-{'a' * 40}",
    ]
    assert tags.ghcr == [  # nosec B101
        "ghcr.io/wgross19/sure-aio:latest",
        "ghcr.io/wgross19/sure-aio:0.7.0",
        "ghcr.io/wgross19/sure-aio:0.7.0-aio.1",
        f"ghcr.io/wgross19/sure-aio:sha-{'a' * 40}",
    ]


def test_compute_registry_tags_tolerates_missing_release_commit(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        registry,
        "find_release_target_commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit(1)),
    )

    tags = registry.compute_registry_tags(repo, sha="b" * 40)

    assert tags.dockerhub == [  # nosec B101
        f"wgross19/sure-aio:sha-{'b' * 40}",
    ]
    assert tags.ghcr == [  # nosec B101
        f"ghcr.io/wgross19/sure-aio:sha-{'b' * 40}",
    ]


def test_upstream_aio_track_release_tag_matches_changelog(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    sha = "c" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: sha
    )

    tags = registry.compute_registry_tags(repo, sha=sha)

    assert tags.release_package_tag == "0.7.0-aio.1"  # nosec B101
    assert "wgross19/sure-aio:0.7.0-aio.1" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/sure-aio:0.7.0-aio.1" in tags.ghcr  # nosec B101


def test_upstream_aio_track_release_tag_allows_existing_v_prefix(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    sha = "d" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "2.15.3"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v2.15.3-aio.2",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: sha
    )

    tags = registry.compute_registry_tags(repo, sha=sha)

    assert tags.release_package_tag == "v2.15.3-aio.2"  # nosec B101
    assert "wgross19/sure-aio:v2.15.3-aio.2" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/sure-aio:v2.15.3-aio.2" in tags.ghcr  # nosec B101


def test_release_tag_allows_changelog_format_followup(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.2",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(release): format sure changelog"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "CHANGELOG.md"
        if (command, *args[:2]) == (
            "diff",
            "--name-status",
            f"{release_sha}..{publish_sha}",
        ):
            return "M	CHANGELOG.md"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha)

    assert tags.release_package_tag == "0.7.0-aio.2"  # nosec B101
    assert "wgross19/sure-aio:0.7.0-aio.2" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/sure-aio:0.7.0-aio.2" in tags.ghcr  # nosec B101


def test_registry_sha_tag_required_for_non_publish_manifest_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.2",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(fleet): reconcile app manifest"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return ".aio-fleet.yml"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    include_sha_tag = registry.registry_sha_tag_required(
        repo, component="aio", sha=publish_sha
    )
    tags = registry.compute_registry_tags(
        repo, sha=publish_sha, component="aio", include_sha_tag=include_sha_tag
    )

    assert include_sha_tag is False  # nosec B101
    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/sure-aio:0.7.0-aio.2" not in tags.dockerhub  # nosec B101
    assert f"wgross19/sure-aio:sha-{publish_sha}" not in tags.dockerhub  # nosec B101


def test_registry_sha_tag_skips_validation_only_followup(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("simplelogin-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_component_release_target_commit", lambda *_args: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "tests/helpers.py"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    assert (  # nosec B101
        registry.registry_sha_tag_required(repo, component="aio", sha=publish_sha)
        is False
    )


def test_registry_sha_tag_skips_other_component_release_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_component_release_target_commit", lambda *_args: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "\n".join(
                [
                    "CHANGELOG.alpha.md",
                    "Dockerfile.alpha",
                    "sure-aio-alpha.xml",
                    "tests/test_alpha_lane_assets.py",
                ]
            )
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    assert (  # nosec B101
        registry.registry_sha_tag_required(repo, component="aio", sha=publish_sha)
        is False
    )


def test_sidecar_sha_tag_skips_unrelated_aio_release_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("signoz-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_component_release_target_commit", lambda *_args: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "\n".join(
                [
                    ".aio-fleet.yml",
                    "CHANGELOG.md",
                    "Dockerfile",
                    "signoz-aio.xml",
                    "tests/helpers.py",
                ]
            )
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    assert (  # nosec B101
        registry.registry_sha_tag_required(repo, component="agent", sha=publish_sha)
        is False
    )


def test_release_tag_rejects_arbitrary_post_release_commit(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.2",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)
    monkeypatch.setattr(registry, "git", lambda *_args: "fix(runtime): later change")

    tags = registry.compute_registry_tags(repo, sha=publish_sha)

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/sure-aio:0.7.0-aio.2" not in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/sure-aio:0.7.0-aio.2" not in tags.ghcr  # nosec B101


def test_release_tag_rejects_changelog_format_subject_with_runtime_changes(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    release_sha = "c" * 40
    publish_sha = "d" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.7.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.7.0-aio.2",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(release): format sure changelog"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "CHANGELOG.md\nDockerfile"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha)

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/sure-aio:0.7.0-aio.2" not in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/sure-aio:0.7.0-aio.2" not in tags.ghcr  # nosec B101


def test_component_release_tag_uses_component_suffix(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("signoz-aio")
    sha = "e" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.152.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "0.152.0-agent.1",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: sha
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="agent")

    assert tags.release_package_tag == "0.152.0-agent.1"  # nosec B101
    assert "wgross19/signoz-agent:0.152.0-agent.1" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/signoz-agent:0.152.0-agent.1" in tags.ghcr  # nosec B101


def test_registry_only_component_uses_alpha_floating_tag(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    sha = "a" * 40

    monkeypatch.setattr(
        registry,
        "_read_component_upstream_version",
        lambda *_args, **_kwargs: "0.7.1-alpha.7",
    )
    monkeypatch.setattr(registry, "_read_component_arg", lambda *_args, **_kwargs: "1")
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: sha
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert tags.release_package_tag == "0.7.1-alpha.7-aio.1"  # nosec B101
    assert tags.dockerhub == [  # nosec B101
        "wgross19/sure-aio-alpha:latest-alpha",
        "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1",
    ]
    assert tags.ghcr == [  # nosec B101
        "ghcr.io/wgross19/sure-aio-alpha:latest-alpha",
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1",
    ]


def test_registry_only_prerelease_component_uses_sync_commit_tags(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.10\n" "ARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "## 0.7.1-alpha.10-aio.1\n\n- Track alpha 10.\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(sync): bump sure alpha to 0.7.1-alpha.10")
    sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
            "components": {
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "upstream_config": "upstream.toml",
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_suffix": "aio",
                    "registry_revision_arg": "AIO_REVISION",
                    "floating_tags": ["latest-alpha"],
                    "include_upstream_version_tag": False,
                    "include_sha_tag": False,
                }
            },
        },
        defaults={},
        owner="wgross19",
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert tags.release_package_tag == "0.7.1-alpha.10-aio.1"  # nosec B101
    assert tags.dockerhub == [  # nosec B101
        "wgross19/sure-aio-alpha:latest-alpha",
        "wgross19/sure-aio-alpha:0.7.1-alpha.10-aio.1",
    ]
    assert tags.ghcr == [  # nosec B101
        "ghcr.io/wgross19/sure-aio-alpha:latest-alpha",
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.10-aio.1",
    ]


def test_registry_only_component_keeps_tags_after_non_release_followup(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.9\n" "ARG AIO_REVISION=2\n"
    )
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.alpha.md").write_text("## 0.7.1-alpha.9-aio.2\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.1-alpha.9-aio.2")
    (repo_path / ".aio-fleet.yml").write_text("schema_version: 1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile app manifest")
    sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
            "components": {
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "upstream_config": "upstream.toml",
                    "release_policy": "registry_only",
                    "release_suffix": "aio",
                    "registry_revision_arg": "AIO_REVISION",
                    "floating_tags": ["latest-alpha"],
                    "include_upstream_version_tag": False,
                    "include_sha_tag": False,
                }
            },
        },
        defaults={"non_release_paths": [".aio-fleet.yml"]},
        owner="wgross19",
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert tags.release_package_tag == "0.7.1-alpha.9-aio.2"  # nosec B101
    assert tags.dockerhub == [  # nosec B101
        "wgross19/sure-aio-alpha:latest-alpha",
        "wgross19/sure-aio-alpha:0.7.1-alpha.9-aio.2",
    ]
    assert tags.ghcr == [  # nosec B101
        "ghcr.io/wgross19/sure-aio-alpha:latest-alpha",
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.9-aio.2",
    ]


def test_registry_only_component_sha_tag_skips_helper_followup_without_release_tag(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "nanoclaw-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    agent = repo_path / "components" / "nanoclaw-agent"
    agent.mkdir(parents=True)
    (agent / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=v2.0.64\n" "ARG AGENT_REVISION=2\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(sync): update nanoclaw agent")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    helpers = repo_path / "tests" / "helpers.py"
    helpers.parent.mkdir()
    helpers.write_text("# shared helper update\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "test(smoke): use shared app test helpers")
    publish_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    repo = RepoConfig(
        name="nanoclaw-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "nanoclaw-aio",
            "image_name": "wgross19/nanoclaw-aio",
            "docker_cache_scope": "nanoclaw-aio-image",
            "pytest_image_tag": "nanoclaw-aio:pytest",
            "publish_profile": "multi-component",
            "components": {
                "aio": {
                    "image_name": "wgross19/nanoclaw-aio",
                    "dockerfile": "Dockerfile",
                },
                "agent": {
                    "image_name": "wgross19/nanoclaw-agent",
                    "dockerfile": "components/nanoclaw-agent/Dockerfile",
                    "release_policy": "registry_only",
                    "release_suffix": "agent",
                    "registry_revision_arg": "AGENT_REVISION",
                },
            },
        },
        defaults={"non_release_paths": ["tests/**"]},
        owner="wgross19",
    )

    include_sha_tag = registry.registry_sha_tag_required(
        repo, component="agent", sha=publish_sha
    )

    assert release_sha != publish_sha  # nosec B101
    assert include_sha_tag is False  # nosec B101


def test_registry_only_component_release_tag_requires_allowed_sha(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    sha = "a" * 40

    monkeypatch.setattr(
        registry,
        "_read_component_upstream_version",
        lambda *_args, **_kwargs: "0.7.1-alpha.7",
    )
    monkeypatch.setattr(registry, "_read_component_arg", lambda *_args, **_kwargs: "1")
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: "b" * 40
    )
    monkeypatch.setattr(
        registry, "_release_tag_sha_allowed", lambda *_args, **_kwargs: False
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/sure-aio-alpha:latest-alpha" not in tags.dockerhub  # nosec B101
    assert (
        "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" not in tags.dockerhub
    )  # nosec B101
    assert (
        "ghcr.io/wgross19/sure-aio-alpha:latest-alpha" not in tags.ghcr
    )  # nosec B101
    assert (
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" not in tags.ghcr
    )  # nosec B101


def test_registry_only_component_omits_upstream_tag_when_release_sha_denied(
    monkeypatch,
) -> None:
    base = load_manifest(FIXTURE).repo("sure-aio")
    raw = dict(base.raw)
    components = dict(raw["components"])
    sure_alpha = dict(components["sure-alpha"])
    sure_alpha["include_upstream_version_tag"] = True
    components["sure-alpha"] = sure_alpha
    raw["components"] = components
    repo = RepoConfig(name=base.name, raw=raw, defaults=base.defaults, owner=base.owner)
    sha = "a" * 40

    monkeypatch.setattr(
        registry,
        "_read_component_upstream_version",
        lambda *_args, **_kwargs: "0.7.1-alpha.7",
    )
    monkeypatch.setattr(registry, "_read_component_arg", lambda *_args, **_kwargs: "1")
    monkeypatch.setattr(
        registry, "_release_tag_sha_allowed", lambda *_args, **_kwargs: False
    )

    tags = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/sure-aio-alpha:latest-alpha" not in tags.dockerhub  # nosec B101
    assert "wgross19/sure-aio-alpha:0.7.1-alpha.7" not in tags.dockerhub  # nosec B101
    assert (
        "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" not in tags.dockerhub
    )  # nosec B101


def test_sure_alpha_and_stable_tags_are_disjoint(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    sha = "a" * 40

    def fake_version(_repo: object, component: str = "aio") -> str:
        return "0.7.1-alpha.7" if component == "sure-alpha" else "0.7.0-hotfix.3"

    def fake_release_tag(_repo: object, *, component: str = "aio", **_kwargs) -> str:
        return (
            "0.7.1-alpha.7-aio.1"
            if component == "sure-alpha"
            else "0.7.0-hotfix.3-aio.1"
        )

    monkeypatch.setattr(registry, "_read_component_upstream_version", fake_version)
    monkeypatch.setattr(registry, "_release_package_tag", fake_release_tag)
    monkeypatch.setattr(registry, "_read_component_arg", lambda *_args: "1")

    stable = registry.compute_registry_tags(repo, sha=sha, component="aio")
    alpha = registry.compute_registry_tags(repo, sha=sha, component="sure-alpha")

    assert set(stable.all_tags).isdisjoint(alpha.all_tags)  # nosec B101
    assert "wgross19/sure-aio:latest" in stable.dockerhub  # nosec B101
    assert "wgross19/sure-aio-alpha:latest-alpha" in alpha.dockerhub  # nosec B101
    assert (
        "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" in alpha.dockerhub
    )  # nosec B101
    assert f"wgross19/sure-aio:sha-{sha}" in stable.dockerhub  # nosec B101
    assert "wgross19/sure-aio-alpha:0.7.1-alpha.7" not in alpha.dockerhub  # nosec B101
    assert (
        f"wgross19/sure-aio-alpha:sha-alpha-{sha}" not in alpha.dockerhub
    )  # nosec B101
    assert len(alpha.dockerhub) == 2  # nosec B101
    assert len(alpha.ghcr) == 2  # nosec B101
    assert all(  # nosec B101
        tag.startswith("wgross19/sure-aio-alpha:") for tag in alpha.dockerhub
    )
    assert all(  # nosec B101
        tag.startswith("ghcr.io/wgross19/sure-aio-alpha:") for tag in alpha.ghcr
    )


def test_component_release_tag_rejects_other_component_release_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("signoz-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v0.124.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v0.124.0-aio.1",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "\n".join(
                [
                    "chore(release): 0.152.0-agent.1 (#71)",
                    "chore(release): format signoz changelog",
                ]
            )
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "CHANGELOG.md\nsignoz-agent.xml\n.aio-fleet.yml"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha, component="aio")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/signoz-aio:v0.124.0-aio.1" not in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/signoz-aio:v0.124.0-aio.1" not in tags.ghcr  # nosec B101


def test_component_release_tag_rejects_other_component_runtime_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("signoz-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v0.124.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v0.124.0-aio.1",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(sync): update signoz agent"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "components/signoz-agent/Dockerfile"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha, component="aio")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/signoz-aio:v0.124.0-aio.1" not in tags.dockerhub  # nosec B101
    assert "wgross19/signoz-aio:v0.124.0" not in tags.dockerhub  # nosec B101


def test_component_release_tag_allows_centralized_cleanup_followup(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("nanoclaw-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v2.0.64"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v2.0.64-aio.3",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(cleanup): remove centralized repo files (#44)"
        cleanup_paths = [
            ".github/FUNDING.yml",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/pull_request_template.md",
            "SECURITY.md",
            "cliff.toml",
            "renovate.json",
            "upstream.toml",
        ]
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "\n".join(cleanup_paths)
        if (command, *args[:2]) == (
            "diff",
            "--name-status",
            f"{release_sha}..{publish_sha}",
        ):
            return "\n".join(f"D\t{path}" for path in cleanup_paths)
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha, component="aio")

    assert tags.release_package_tag == "v2.0.64-aio.3"  # nosec B101
    assert "wgross19/nanoclaw-aio:v2.0.64-aio.3" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/nanoclaw-aio:v2.0.64-aio.3" in tags.ghcr  # nosec B101


def test_component_release_tag_rejects_cleanup_subject_with_runtime_changes(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("nanoclaw-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v2.0.64"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v2.0.64-aio.3",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(cleanup): remove centralized repo files (#44)"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return "Dockerfile"
        if (command, *args[:2]) == (
            "diff",
            "--name-status",
            f"{release_sha}..{publish_sha}",
        ):
            return "A\tDockerfile"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha, component="aio")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/nanoclaw-aio:v2.0.64-aio.3" not in tags.dockerhub  # nosec B101
    assert "wgross19/nanoclaw-aio:v2.0.64" not in tags.dockerhub  # nosec B101


def test_signoz_agent_publish_command_uses_component_context(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("signoz-aio")

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "0.151.0"
    )
    monkeypatch.setattr(
        registry, "_release_package_tag", lambda *_args, **_kwargs: "0.151.0-agent.1"
    )

    command = registry_publish_command(repo, sha="c" * 40, component="agent")

    assert "--file" in command  # nosec B101
    assert "components/signoz-agent/Dockerfile" in command  # nosec B101
    assert command[-1] == "components/signoz-agent"  # nosec B101
    assert "wgross19/signoz-agent:0.151.0" in command  # nosec B101


def test_nanoclaw_component_tags_use_paired_release_model(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("nanoclaw-aio")
    sha = "f" * 40

    def fake_version(_repo: object, component: str = "aio") -> str:
        return "v2.0.63"

    def fake_release_tag(_repo: object, *, component: str = "aio", **_kwargs) -> str:
        return "v2.0.63-aio.1" if component == "aio" else "v2.0.63-agent.1"

    monkeypatch.setattr(registry, "_read_component_upstream_version", fake_version)
    monkeypatch.setattr(registry, "_release_package_tag", fake_release_tag)
    monkeypatch.setattr(registry, "_read_component_arg", lambda *_args: "1")

    aio = registry.compute_registry_tags(repo, sha=sha, component="aio")
    agent = registry.compute_registry_tags(repo, sha=sha, component="agent")

    assert aio.dockerhub == [  # nosec B101
        "wgross19/nanoclaw-aio:latest",
        "wgross19/nanoclaw-aio:v2.0.63",
        "wgross19/nanoclaw-aio:v2.0.63-aio.1",
        f"wgross19/nanoclaw-aio:sha-{sha}",
    ]
    assert agent.dockerhub == [  # nosec B101
        "wgross19/nanoclaw-agent:latest",
        "wgross19/nanoclaw-agent:v2.0.63",
        "wgross19/nanoclaw-agent:v2.0.63-agent.1",
        f"wgross19/nanoclaw-agent:sha-{sha}",
    ]
    assert set(aio.all_tags).isdisjoint(agent.all_tags)  # nosec B101


def test_nanoclaw_agent_publish_command_uses_component_context(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("nanoclaw-aio")

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v2.0.63"
    )
    monkeypatch.setattr(
        registry, "_release_package_tag", lambda *_args, **_kwargs: "v2.0.63-agent.1"
    )

    command = registry_publish_command(repo, sha="c" * 40, component="agent")

    assert "--file" in command  # nosec B101
    assert "components/nanoclaw-agent/Dockerfile" in command  # nosec B101
    assert command[-1] == "components/nanoclaw-agent"  # nosec B101
    assert "wgross19/nanoclaw-agent:v2.0.63" in command  # nosec B101
    assert "wgross19/nanoclaw-aio:v2.0.63" not in command  # nosec B101


def test_component_registry_tags_use_configured_component_image(
    tmp_path: Path, monkeypatch
) -> None:
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
    components:
      worker:
        image_name: wgross19/example-worker
        dockerfile: services/worker/Dockerfile
        context: services/worker
""")
    repo = load_manifest(manifest).repo("example-aio")
    monkeypatch.setattr(registry, "_read_component_upstream_version", lambda *_: "")
    monkeypatch.setattr(registry, "_release_package_tag", lambda *_args, **_kwargs: "")

    tags = registry.compute_registry_tags(repo, sha="d" * 40, component="worker")
    command = registry_publish_command(repo, sha="d" * 40, component="worker")

    assert tags.dockerhub == [  # nosec B101
        "wgross19/example-worker:latest",
        f"wgross19/example-worker:sha-{'d' * 40}",
    ]
    assert "wgross19/example-worker:latest" in command  # nosec B101
    assert "wgross19/example-aio:latest" not in command  # nosec B101


def test_registry_publish_command_adds_oci_source_annotations() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    command = registry_publish_command(repo, sha="a" * 40, component="sure-alpha")

    assert "--annotation" in command  # nosec B101
    assert (  # nosec B101
        "index:org.opencontainers.image.source=https://github.com/wgross19/sure-aio"
        in command
    )
    assert (  # nosec B101
        "index:org.opencontainers.image.description="
        "Unstable Unraid-first Sure AIO alpha testing image" in command
    )


def test_dockerhub_verification_uses_docker_imagetools_first(monkeypatch) -> None:
    seen_commands: list[list[str]] = []
    inspect_env = {"DOCKER_CONFIG": "/workspace/aio-fleet-docker"}
    seen_envs: list[dict[str, str] | None] = []
    seen_timeouts: list[int | None] = []
    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")

    def fake_run(command: list[str], **kwargs):
        seen_commands.append(command)
        seen_envs.append(kwargs.get("env"))
        seen_timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    monkeypatch.setattr(
        registry.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Docker Hub tags should use docker inspect before API")
        ),
    )

    assert (
        registry.verify_registry_tags(["wgross19/sure-aio:latest"], env=inspect_env)
        == []
    )  # nosec B101
    assert seen_commands == [  # nosec B101
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "wgross19/sure-aio:latest",
        ]
    ]
    assert seen_envs == [inspect_env]  # nosec B101
    assert seen_timeouts == [registry.REGISTRY_IMAGETOOLS_TIMEOUT_SECONDS]  # nosec B101


def test_registry_verification_reports_docker_inspect_timeout(monkeypatch) -> None:
    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")

    def fake_run(_command: list[str], **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker"], timeout=1)

    monkeypatch.setattr(registry.subprocess, "run", fake_run)
    monkeypatch.setattr(
        registry.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Docker Hub API fallback should not run after timeout")
        ),
    )

    failures = registry.verify_registry_tags(["wgross19/sure-aio:latest"])

    assert len(failures) == 1  # nosec B101
    assert "timed out" in failures[0]  # nosec B101


def test_dockerhub_verification_reports_malformed_json(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        registry.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not indexed yet"
        ),
    )
    monkeypatch.setattr(
        registry.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    monkeypatch.setattr(registry.time, "sleep", lambda _seconds: None)

    failures = registry.verify_registry_tags(["wgross19/sure-aio:latest"])

    assert len(failures) == 1  # nosec B101
    assert failures[0].startswith(  # nosec B101
        "wgross19/sure-aio:latest: Docker Hub tag lookup failed: "
        "invalid Docker Hub JSON response"
    )


def test_dockerhub_verification_reports_response_read_error(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            raise OSError("truncated response body")

    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        registry.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not indexed yet"
        ),
    )
    monkeypatch.setattr(
        registry.urllib.request, "urlopen", lambda *_args, **_kwargs: Response()
    )
    monkeypatch.setattr(registry.time, "sleep", lambda _seconds: None)

    failures = registry.verify_registry_tags(["wgross19/sure-aio:latest"])

    assert len(failures) == 1  # nosec B101
    assert failures[0].startswith(  # nosec B101
        "wgross19/sure-aio:latest: Docker Hub tag lookup failed: "
        "invalid Docker Hub JSON response: truncated response body"
    )


def test_dockerhub_verification_reports_missing_tag(monkeypatch) -> None:
    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        registry.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not found"
        ),
    )
    monkeypatch.setattr(registry.time, "sleep", lambda _seconds: None)

    def fake_urlopen(url: str, timeout: int):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)

    assert registry.verify_registry_tags(
        ["wgross19/sure-aio:missing"]
    ) == [  # nosec B101
        "wgross19/sure-aio:missing: tag not found on Docker Hub"
    ]


def test_dockerhub_verification_retries_new_tag_404(monkeypatch) -> None:
    attempts = 0
    sleeps: list[int] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(url: str, timeout: int):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HTTPError(url, 404, "Not Found", {}, None)
        return Response()

    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(
        registry.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not indexed yet"
        ),
    )
    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(registry.time, "sleep", sleeps.append)

    assert (
        registry.verify_registry_tags(["wgross19/sure-aio:sha-new"]) == []
    )  # nosec B101
    assert attempts == 3  # nosec B101
    assert sleeps == [2, 4]  # nosec B101


def test_delete_dockerhub_tags_deletes_only_guarded_tags(monkeypatch) -> None:
    deleted_urls: list[str] = []

    class Response:
        def __init__(self, status: int, body: bytes = b"{}") -> None:
            self.status = status
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(request, timeout: int):
        del timeout
        if request.full_url == "https://hub.docker.com/v2/auth/token":
            assert json.loads(request.data.decode()) == {  # nosec B101
                "identifier": "wgross19",
                "secret": "hub-token",
            }
            return Response(200, b'{"access_token":"hub-jwt"}')
        assert request.get_method() == "DELETE"  # nosec B101
        assert request.headers["Authorization"] == "Bearer hub-jwt"  # nosec B101
        deleted_urls.append(request.full_url)
        return Response(204)

    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)

    results = registry.delete_dockerhub_tags(
        image="wgross19/sure-aio",
        tags=["latest-alpha", "latest-alpha", "0.7.1-alpha.7-aio.4"],
        username="wgross19",
        token="hub-token",
        required_substring="alpha",
    )

    assert results == [  # nosec B101
        {"tag": "latest-alpha", "state": "deleted"},
        {"tag": "0.7.1-alpha.7-aio.4", "state": "deleted"},
    ]
    assert deleted_urls == [  # nosec B101
        "https://hub.docker.com/v2/namespaces/wgross19/repositories/sure-aio/tags/latest-alpha",
        "https://hub.docker.com/v2/namespaces/wgross19/repositories/sure-aio/tags/0.7.1-alpha.7-aio.4",
    ]


def test_delete_dockerhub_tags_refuses_unguarded_tag() -> None:
    try:
        registry.delete_dockerhub_tags(
            image="wgross19/sure-aio",
            tags=["latest"],
            username="wgross19",
            token="hub-token",
            required_substring="alpha",
        )
    except ValueError as error:
        assert "refusing to delete tag without required substring" in str(  # nosec B101
            error
        )
    else:
        raise AssertionError("expected guarded delete to reject stable tag")


def test_delete_dockerhub_tags_requires_delete_token() -> None:
    try:
        registry.delete_dockerhub_tags(
            image="wgross19/sure-aio",
            tags=["latest-alpha"],
            username="wgross19",
            token="",
            required_substring="alpha",
        )
    except ValueError as error:
        assert "DOCKERHUB_DELETE_TOKEN" in str(error)  # nosec B101
    else:
        raise AssertionError("expected cleanup credential refusal")


def test_delete_dockerhub_tags_rejects_image_with_url_metacharacters() -> None:
    for image in ["wgross19/sure-aio#", "wgross19/sure-aio?x=1"]:
        try:
            registry.delete_dockerhub_tags(
                image=image,
                tags=["latest-alpha"],
                username="wgross19",
                token="hub-token",
                required_substring="alpha",
                dry_run=True,
            )
        except ValueError as error:
            assert "unsupported Docker Hub image format" in str(error)  # nosec B101
        else:
            raise AssertionError(f"expected invalid Docker Hub image to fail: {image}")


def test_delete_dockerhub_tags_reports_forbidden_permission(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"access_token":"hub-jwt"}'

    def fake_urlopen(request, timeout: int):
        del timeout
        if request.full_url == "https://hub.docker.com/v2/auth/token":
            return Response()
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)

    try:
        registry.delete_dockerhub_tags(
            image="wgross19/sure-aio",
            tags=["latest-alpha"],
            username="wgross19",
            token="hub-token",
            required_substring="alpha",
        )
    except RuntimeError as error:
        message = str(error)
        assert "Docker Hub delete forbidden" in message  # nosec B101
        assert "lacks tag delete/admin permission" in message  # nosec B101
    else:
        raise AssertionError("expected forbidden delete to report token permission")


def test_dockerhub_auth_preflight_uses_current_token_api(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"access_token":"hub-jwt"}'

    def fake_urlopen(request, timeout: int):
        del timeout
        assert request.full_url == "https://hub.docker.com/v2/auth/token"  # nosec B101
        assert json.loads(request.data.decode()) == {  # nosec B101
            "identifier": "wgross19",
            "secret": "hub-token",
        }
        return Response()

    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)

    failure = registry.dockerhub_auth_preflight_failure(
        username="wgross19",
        token="hub-token",
    )

    assert failure is None  # nosec B101


def test_dockerhub_delete_scope_preflight_reports_forbidden(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"access_token":"hub-jwt"}'

    def fake_urlopen(request, timeout: int):
        del timeout
        if request.full_url == "https://hub.docker.com/v2/auth/token":
            return Response()
        assert request.full_url == (  # nosec B101
            "https://hub.docker.com/v2/namespaces/wgross19/"
            "repositories/sure-aio-alpha/tags/missing-probe"
        )
        assert request.get_method() == "DELETE"  # nosec B101
        raise HTTPError(request.full_url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr(registry.urllib.request, "urlopen", fake_urlopen)

    failure = registry.dockerhub_delete_scope_preflight_failure(
        image="wgross19/sure-aio-alpha",
        username="wgross19",
        token="delete-token",
        probe_tag="missing-probe",
    )

    assert failure is not None  # nosec B101
    assert "DOCKERHUB_DELETE_TOKEN" in failure  # nosec B101
    assert "delete/admin permission" in failure  # nosec B101


def test_ghcr_verification_uses_docker_imagetools(monkeypatch) -> None:
    seen_commands: list[list[str]] = []
    inspect_env = {"DOCKER_CONFIG": "/workspace/aio-fleet-docker"}
    seen_envs: list[dict[str, str] | None] = []
    seen_timeouts: list[int | None] = []
    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")

    def fake_run(command: list[str], **kwargs):
        seen_commands.append(command)
        seen_envs.append(kwargs.get("env"))
        seen_timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(registry.subprocess, "run", fake_run)

    assert (
        registry.verify_registry_tags(
            ["ghcr.io/wgross19/sure-aio:latest"], env=inspect_env
        )
        == []
    )  # nosec B101
    assert seen_commands == [  # nosec B101
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "ghcr.io/wgross19/sure-aio:latest",
        ]
    ]
    assert seen_envs == [inspect_env]  # nosec B101
    assert seen_timeouts == [registry.REGISTRY_IMAGETOOLS_TIMEOUT_SECONDS]  # nosec B101


def test_registry_verification_caches_successes_in_process(monkeypatch) -> None:
    seen_commands: list[list[str]] = []
    monkeypatch.setattr(registry.shutil, "which", lambda _name: "docker")

    def fake_run(command: list[str], **_kwargs):
        seen_commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(registry.subprocess, "run", fake_run)

    assert (  # nosec B101
        registry.verify_registry_tags(["ghcr.io/wgross19/sure-aio:latest"]) == []
    )
    assert (  # nosec B101
        registry.verify_registry_tags(["ghcr.io/wgross19/sure-aio:latest"]) == []
    )
    assert len(seen_commands) == 1  # nosec B101


def test_changelog_version_profile_uses_latest_changelog_heading(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("khoj-aio")
    sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "1.0.0"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit(1)),
    )
    monkeypatch.setattr(
        registry, "latest_changelog_version", lambda *_args, **_kwargs: "app-2024.05"
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: sha
    )

    tags = registry.compute_registry_tags(repo, sha=sha)

    assert tags.release_package_tag == "app-2024.05"  # nosec B101
    assert "wgross19/khoj-aio:app-2024.05" in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/khoj-aio:app-2024.05" in tags.ghcr  # nosec B101


def test_component_release_tag_rejects_cleanup_rename_into_allowed_path(
    monkeypatch,
) -> None:
    repo = load_manifest(FIXTURE).repo("nanoclaw-aio")
    release_sha = "e" * 40
    publish_sha = "f" * 40

    monkeypatch.setattr(
        registry, "_read_component_upstream_version", lambda *_: "v2.0.64"
    )
    monkeypatch.setattr(
        registry,
        "latest_component_changelog_version",
        lambda *_args, **_kwargs: "v2.0.64-aio.3",
    )
    monkeypatch.setattr(
        registry, "find_release_target_commit", lambda *_args, **_kwargs: release_sha
    )
    monkeypatch.setattr(registry, "git_is_ancestor", lambda *_args: True)

    def fake_git(_path: Path, command: str, *args: str) -> str:
        if (command, *args[:2]) == (
            "log",
            "--format=%s",
            f"{release_sha}..{publish_sha}",
        ):
            return "chore(cleanup): move centralized repo files (#44)"
        if (command, *args[:2]) == (
            "diff",
            "--name-only",
            f"{release_sha}..{publish_sha}",
        ):
            return ".github/workflows/entrypoint.sh"
        if (command, *args[:2]) == (
            "diff",
            "--name-status",
            f"{release_sha}..{publish_sha}",
        ):
            return "R100\trootfs/entrypoint.sh\t.github/workflows/entrypoint.sh"
        raise AssertionError(f"unexpected git call: {(command, *args)}")

    monkeypatch.setattr(registry, "git", fake_git)

    tags = registry.compute_registry_tags(repo, sha=publish_sha, component="aio")

    assert tags.release_package_tag == ""  # nosec B101
    assert "wgross19/nanoclaw-aio:v2.0.64-aio.3" not in tags.dockerhub  # nosec B101
    assert "ghcr.io/wgross19/nanoclaw-aio:v2.0.64-aio.3" not in tags.ghcr  # nosec B101
