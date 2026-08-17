from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
from pathlib import Path
from types import SimpleNamespace

from aio_fleet import release_plan as release_plan_module
from aio_fleet.manifest import RepoConfig, load_manifest
from aio_fleet.release_plan import (
    release_plan_for_manifest,
    release_plan_for_repo,
)


def test_release_plan_classifies_publish_missing(tmp_path: Path, monkeypatch) -> None:
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr("aio_fleet.release_plan._git_head", lambda _path: "a" * 40)
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_latest_aio_tag", lambda _repo: "0.7.0-aio.1"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_next_aio", lambda _repo: "0.7.0-aio.2"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "0.7.0-aio.1"},
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.compute_registry_tags",
        lambda *_args, **_kwargs: SimpleNamespace(
            dockerhub=["wgross19/sure-aio:latest"],
            ghcr=["ghcr.io/wgross19/sure-aio:latest"],
            all_tags=["wgross19/sure-aio:latest"],
        ),
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.verify_registry_tags",
        lambda _tags, **_kwargs: ["wgross19/sure-aio:latest: missing"],
    )

    plan = release_plan_for_repo(repo, include_registry=True)

    assert plan["state"] == "publish-missing"  # nosec B101
    assert plan["registry_verified"] is True  # nosec B101
    assert plan["provenance"] == "remote-confirmed"  # nosec B101
    assert plan["registry_failure_evidence"] == [  # nosec B101
        {
            "failure": "wgross19/sure-aio:latest: missing",
            "provenance": "remote-confirmed",
        }
    ]
    assert plan["blockers"] == ["missing or unreachable registry tags"]  # nosec B101
    assert plan["next_action"] == (  # nosec B101
        "uv run aio-fleet release transaction --repo sure-aio "
        f"--component aio --sha {'a' * 40} --dry-run"
    )


def test_release_plan_classifies_sha_tag_gap_separately(
    tmp_path: Path, monkeypatch
) -> None:
    sha = "a" * 40
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr("aio_fleet.release_plan._git_head", lambda _path: sha)
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_latest_aio_tag", lambda _repo: "0.7.0-aio.1"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_next_aio", lambda _repo: "0.7.0-aio.2"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "0.7.0-aio.1"},
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.compute_registry_tags",
        lambda *_args, **_kwargs: SimpleNamespace(
            dockerhub=[f"wgross19/sure-aio:sha-{sha}"],
            ghcr=[f"ghcr.io/wgross19/sure-aio:sha-{sha}"],
            all_tags=[f"wgross19/sure-aio:sha-{sha}"],
        ),
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.verify_registry_tags",
        lambda _tags, **_kwargs: [f"wgross19/sure-aio:sha-{sha}: missing"],
    )

    plan = release_plan_for_repo(repo, include_registry=True)

    assert plan["state"] == "sha-tag-missing"  # nosec B101
    assert plan["registry_state"] == "sha-tag-missing"  # nosec B101
    assert plan["registry_verified"] is True  # nosec B101
    assert plan["blockers"] == []  # nosec B101
    assert plan["warnings"] == [  # nosec B101
        "sha registry tag missing for current source commit"
    ]
    assert plan["next_action"] == (  # nosec B101
        f"python -m aio_fleet registry verify --repo sure-aio "
        f"--component aio --sha {sha} --verbose"
    )


def test_release_plan_stays_current_when_sha_tag_is_not_required(
    tmp_path: Path, monkeypatch
) -> None:
    sha = "a" * 40
    repo = RepoConfig(
        name="simplelogin-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "simplelogin-aio",
            "image_name": "wgross19/simplelogin-aio",
            "docker_cache_scope": "simplelogin-aio-image",
            "pytest_image_tag": "simplelogin-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr("aio_fleet.release_plan._git_head", lambda _path: sha)
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_latest_aio_tag", lambda _repo: "5.0.0-aio.1"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_next_aio", lambda _repo: "5.0.0-aio.2"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_changelog_version",
        lambda _repo, *, component="aio": "5.0.0-aio.1",
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "5.0.0-aio.1"},
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.registry_sha_tag_required",
        lambda *_args, **_kwargs: False,
    )

    def fake_compute_registry_tags(*_args, **kwargs):
        assert kwargs["include_sha_tag"] is False  # nosec B101
        return SimpleNamespace(
            dockerhub=["wgross19/simplelogin-aio:latest"],
            ghcr=["ghcr.io/wgross19/simplelogin-aio:latest"],
            all_tags=["wgross19/simplelogin-aio:latest"],
        )

    monkeypatch.setattr(
        "aio_fleet.release_plan.compute_registry_tags", fake_compute_registry_tags
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan.verify_registry_tags", lambda _tags, **_kwargs: []
    )

    plan = release_plan_for_repo(repo, include_registry=True)

    assert plan["state"] == "current"  # nosec B101
    assert plan["registry_state"] == "ok"  # nosec B101
    assert plan["warnings"] == []  # nosec B101


def test_release_plan_classifies_catalog_sync_needed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = RepoConfig(
        name="dify-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "dify-aio",
            "image_name": "wgross19/dify-aio",
            "docker_cache_scope": "dify-aio-image",
            "pytest_image_tag": "dify-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr("aio_fleet.release_plan._git_head", lambda _path: "b" * 40)
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_latest_aio_tag", lambda _repo: "1.14.0-aio.2"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_next_aio", lambda _repo: "1.14.0-aio.3"
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._safe_changelog_version",
        lambda _repo, *, component="aio": "1.14.0-aio.2",
    )
    monkeypatch.setattr(
        "aio_fleet.release_plan._latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "1.14.0-aio.2"},
    )

    plan = release_plan_for_repo(repo, catalog_sync_needed=True)

    assert plan["state"] == "catalog-sync-needed"  # nosec B101
    assert plan["catalog_sync_needed"] is True  # nosec B101


def test_release_plan_retries_transient_registry_miss(
    tmp_path: Path, monkeypatch
) -> None:
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(release_plan_module, "_git_head", lambda _path: "a" * 40)
    monkeypatch.setattr(
        release_plan_module, "_safe_latest_aio_tag", lambda _repo: "0.7.0-aio.1"
    )
    monkeypatch.setattr(
        release_plan_module, "_safe_next_aio", lambda _repo: "0.7.0-aio.2"
    )
    monkeypatch.setattr(
        release_plan_module, "_safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "0.7.0-aio.1"},
    )
    monkeypatch.setattr(
        release_plan_module,
        "compute_registry_tags",
        lambda *_args, **_kwargs: SimpleNamespace(
            dockerhub=["wgross19/sure-aio:latest"],
            ghcr=["ghcr.io/wgross19/sure-aio:latest"],
            all_tags=["wgross19/sure-aio:latest"],
        ),
    )
    calls = 0

    def fake_verify(_tags, **_kwargs):
        nonlocal calls
        calls += 1
        return ["wgross19/sure-aio:latest: missing"] if calls == 1 else []

    monkeypatch.setattr(release_plan_module, "verify_registry_tags", fake_verify)
    release_plan_module._REGISTRY_VERIFY_CACHE.clear()

    plan = release_plan_for_repo(repo, include_registry=True)

    assert calls == 2  # nosec B101
    assert plan["state"] == "current"  # nosec B101
    assert plan["registry_failures"] == []  # nosec B101


def test_release_plan_blocks_unsigned_generated_pr(tmp_path: Path, monkeypatch) -> None:
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "public": True,
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(release_plan_module, "_git_head", lambda _path: "d" * 40)
    monkeypatch.setattr(
        release_plan_module,
        "_safe_latest_aio_tag",
        lambda _repo: "0.7.0-aio.1",
    )
    monkeypatch.setattr(release_plan_module, "_safe_next_aio", lambda _repo: "")
    monkeypatch.setattr(
        release_plan_module, "_safe_has_aio_changes", lambda _repo: False
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.0-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {"state": "ok", "tag": "0.7.0-aio.1"},
    )
    monkeypatch.setattr(
        release_plan_module,
        "generated_pr_signature_blockers",
        lambda _github_repo: ["generated PR #42 has unverified commits: unsigned"],
    )

    plan = release_plan_for_repo(repo)

    assert plan["state"] == "blocked"  # nosec B101
    assert plan["blockers"] == [  # nosec B101
        "generated PR #42 has unverified commits: unsigned"
    ]
    assert plan["next_action"] == (  # nosec B101
        "uv run aio-fleet signing doctor --repo sure-aio --format json"
    )


def test_release_plan_redacts_private_manifest_repos(
    tmp_path: Path, monkeypatch
) -> None:
    private_path = tmp_path / "private-service-aio"
    public_path = tmp_path / "public-service-aio"
    private_path.mkdir()
    public_path.mkdir()
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text(f"""
owner: wgross19
repos:
  private-service-aio:
    path: {private_path}
    github_repo: PrivateOrg/private-service-aio
    public: false
    app_slug: private-service-aio
    image_name: wgross19/private-service-aio
    docker_cache_scope: private-service-aio-image
    pytest_image_tag: private-service-aio:pytest
  public-service-aio:
    path: {public_path}
    github_repo: wgross19/public-service-aio
    public: true
    app_slug: public-service-aio
    image_name: wgross19/public-service-aio
    docker_cache_scope: public-service-aio-image
    pytest_image_tag: public-service-aio:pytest
""")
    calls: list[str] = []

    def fake_release_plan(repo: RepoConfig, **_kwargs):
        calls.append(repo.name)
        return {
            "repo": repo.name,
            "profile": repo.publish_profile,
            "sha": "e" * 40,
            "latest_release_tag": "1.0.0-aio.1",
            "latest_changelog_version": "1.0.0-aio.1",
            "latest_github_release": {
                "state": "ok",
                "tag": "1.0.0-aio.1",
                "url": f"https://github.com/{repo.github_repo}/releases/tag/1.0.0-aio.1",
            },
            "next_version": "",
            "release_due": False,
            "catalog_sync_needed": False,
            "registry_state": "ok",
            "registry_tags": {"dockerhub": [], "ghcr": []},
            "registry_failures": [],
            "state": "current",
            "blockers": [],
            "warnings": [],
            "next_action": "none",
        }

    monkeypatch.setattr(
        "aio_fleet.release_plan.release_plan_for_repo", fake_release_plan
    )

    rows = release_plan_for_manifest(load_manifest(manifest_path), redact_private=True)

    private_row = next(row for row in rows if row["repo"] == "private-service-aio")
    public_row = next(row for row in rows if row["repo"] == "public-service-aio")
    assert calls == ["public-service-aio"]  # nosec B101
    assert private_row["state"] == "private-skipped"  # nosec B101
    assert private_row["sha"] == ""  # nosec B101
    assert private_row["latest_github_release"] == {  # nosec B101
        "state": "private-skipped"
    }
    assert public_row["sha"] == "e" * 40  # nosec B101


def test_latest_github_release_uses_dashboard_token(
    monkeypatch, tmp_path: Path
) -> None:
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(tmp_path),
            "github_repo": "wgross19/sure-aio",
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
        },
        defaults={},
        owner="wgross19",
    )
    captured_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object):
        nonlocal captured_env
        captured_env = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "tagName": "1.0.0-aio.1",
                    "publishedAt": "2026-05-13T00:00:00Z",
                    "targetCommitish": "a" * 40,
                    "url": "https://github.com/wgross19/sure-aio/releases/tag/1.0.0-aio.1",
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("AIO_FLEET_DASHBOARD_TOKEN", "dashboard-token")
    monkeypatch.setenv("AIO_FLEET_UPSTREAM_TOKEN", "upstream-token")
    monkeypatch.setenv("GH_TOKEN", "lower-priority-token")
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setattr(release_plan_module.subprocess, "run", fake_run)

    result = release_plan_module._latest_github_release(repo)

    assert result["state"] == "ok"  # nosec B101
    assert captured_env["GH_TOKEN"] == "dashboard-token"  # nosec B101
    assert "AIO_FLEET_DASHBOARD_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101


def test_release_plan_ignores_registry_only_component_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=0.7.0\n" "ARG UPSTREAM_IMAGE_DIGEST=sha256:stable\n"
    )
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.1\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:alpha\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.0-aio.1")
    _git(repo_path, "tag", "0.7.0-aio.1")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.2\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:alpha2\n"
    )
    (repo_path / "CHANGELOG.alpha.md").write_text("## 0.7.1-alpha.2-aio.1\n")
    (repo_path / "README.md").write_text("Alpha lane docs\n")
    (repo_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(deps): update sure alpha")
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
                "aio": {"image_name": "wgross19/sure-aio"},
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "publish_paths": ["README.md", "pyproject.toml", "rootfs-alpha/**"],
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {"state": "unknown"},
    )
    monkeypatch.setattr(release_plan_module, "_safe_next_aio", lambda _repo: "")
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.0-aio.1",
    )

    plan = release_plan_for_repo(repo)

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def test_release_plan_does_not_treat_shared_paths_as_registry_only(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=0.7.0\n")
    (repo_path / "Dockerfile.alpha").write_text("ARG UPSTREAM_VERSION=0.7.0-alpha.1\n")
    (repo_path / "upstream.toml").write_text('[upstream]\nversion = "0.7.0"\n')
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.0-aio.1")
    _git(repo_path, "tag", "0.7.0-aio.1")
    (repo_path / "upstream.toml").write_text('[upstream]\nversion = "0.7.1"\n')
    _git(repo_path, "add", "upstream.toml")
    _git(repo_path, "commit", "-m", "chore(deps): bump upstream")

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
                "aio": {"dockerfile": "Dockerfile", "upstream_config": "upstream.toml"},
                "sure-alpha": {
                    "dockerfile": "Dockerfile.alpha",
                    "upstream_config": "upstream.toml",
                    "release_policy": "registry_only",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )

    patterns = release_plan_module._registry_only_component_patterns(repo)
    assert "upstream.toml" not in patterns  # nosec B101
    assert (
        release_plan_module._only_registry_only_component_changes(repo) is False
    )  # nosec B101


def test_release_plan_marks_registry_only_component_changes_due(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=0.7.0\n" "ARG UPSTREAM_IMAGE_DIGEST=sha256:stable\n"
    )
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.9\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:alpha\n"
        "ARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "## 0.7.1-alpha.9-aio.1\n\n- Initial alpha package.\n"
    )
    (repo_path / "rootfs-alpha").mkdir()
    (repo_path / "rootfs-alpha" / "overlay.rb").write_text("old\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.1-alpha.9-aio.1")
    _git(repo_path, "tag", "0.7.0-aio.1")
    _git(repo_path, "tag", "sure-alpha/0.7.1-alpha.9-aio.1")
    (repo_path / "rootfs-alpha" / "overlay.rb").write_text("new\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "fix(alpha): harden import preflight")
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
                "aio": {"image_name": "wgross19/sure-aio"},
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                    "registry_revision_arg": "AIO_REVISION",
                    "publish_paths": ["rootfs-alpha/**"],
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {"state": "unknown"},
    )

    plan = release_plan_for_repo(repo, component="sure-alpha")

    assert plan["latest_release_tag"] == "sure-alpha/0.7.1-alpha.9-aio.1"  # nosec B101
    assert plan["next_version"] == "0.7.1-alpha.9-aio.2"  # nosec B101
    assert plan["release_due"] is True  # nosec B101
    assert plan["state"] == "release-due"  # nosec B101


def test_release_plan_registry_only_revision_bump_without_tag_still_due(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=0.7.0\n" "ARG UPSTREAM_IMAGE_DIGEST=sha256:stable\n"
    )
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.9\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:alpha\n"
        "ARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "## 0.7.1-alpha.9-aio.1\n\n- Initial alpha package.\n"
    )
    (repo_path / "rootfs-alpha").mkdir()
    (repo_path / "rootfs-alpha" / "overlay.rb").write_text("old\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.1-alpha.9-aio.1")
    _git(repo_path, "tag", "0.7.0-aio.1")
    _git(repo_path, "tag", "sure-alpha/0.7.1-alpha.9-aio.1")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.9\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:alpha\n"
        "ARG AIO_REVISION=2\n"
    )
    (repo_path / "rootfs-alpha" / "overlay.rb").write_text("new\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "fix(alpha): bump registry revision")
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
                "aio": {"image_name": "wgross19/sure-aio"},
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                    "registry_revision_arg": "AIO_REVISION",
                    "publish_paths": ["rootfs-alpha/**", "Dockerfile.alpha"],
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {"state": "unknown"},
    )

    plan = release_plan_for_repo(repo, component="sure-alpha")

    assert plan["latest_release_tag"] == "sure-alpha/0.7.1-alpha.9-aio.1"  # nosec B101
    assert plan["next_version"] == "0.7.1-alpha.9-aio.2"  # nosec B101
    assert plan["release_due"] is True  # nosec B101
    assert plan["state"] == "release-due"  # nosec B101


def test_changed_paths_since_rejects_untrusted_option_like_ref(tmp_path: Path) -> None:
    repo_path = tmp_path / "safe-repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "tracked.txt").write_text("one\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "init")
    _git(repo_path, "tag", "v1.0.0")
    (repo_path / "tracked.txt").write_text("two\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "update tracked file")

    clobber_path = tmp_path / "git-diff-clobber"
    clobber_path.write_text("keep-me")
    malicious_ref = f"--output={clobber_path}"

    changed = release_plan_module._changed_paths_since(repo_path, malicious_ref)

    assert changed == []  # nosec B101
    assert clobber_path.read_text() == "keep-me"  # nosec B101


def test_release_plan_ignores_retired_shared_file_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "nanoclaw-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.64\n")
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.md").write_text("## v2.0.64-aio.1\n")
    (repo_path / "cliff.toml").write_text("[changelog]\n")
    (repo_path / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (repo_path / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").write_text(
        "name: Bug report\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.0.64-aio.1")
    _git(repo_path, "tag", "v2.0.64-aio.1")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    (repo_path / "cliff.toml").unlink()
    (repo_path / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").unlink()
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(cleanup): remove centralized files")
    repo = RepoConfig(
        name="nanoclaw-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "nanoclaw-aio",
            "image_name": "wgross19/nanoclaw-aio",
            "docker_cache_scope": "nanoclaw-aio-image",
            "pytest_image_tag": "nanoclaw-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "v2.0.64-aio.1",
            "target_commitish": release_sha,
        },
    )

    plan = release_plan_for_repo(repo)

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def test_release_plan_uses_default_non_release_paths_for_docs_only_commits(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "penpot-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG PENPOT_VERSION=2.15.3\n")
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "README.md").write_text("old\n")
    (repo_path / "CHANGELOG.md").write_text("## v2.15.3-aio.1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.15.3-aio.1")
    _git(repo_path, "tag", "v2.15.3-aio.1")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    (repo_path / "README.md").write_text("new\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "docs(readme): refresh project docs")
    repo = RepoConfig(
        name="penpot-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "penpot-aio",
            "image_name": "wgross19/penpot-aio",
            "docker_cache_scope": "penpot-aio-image",
            "pytest_image_tag": "penpot-aio:pytest",
            "publish_profile": "changelog-version",
        },
        defaults={"non_release_paths": ["README.md", "docs/**"]},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "v2.15.3-aio.1",
            "target_commitish": release_sha,
        },
    )

    plan = release_plan_for_repo(repo)

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def test_release_plan_ignores_manifest_only_component_commit(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "signoz-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.0\n")
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.md").write_text("## 1.0.0-agent.1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 1.0.0-agent.1")
    _git(repo_path, "tag", "1.0.0-agent.1")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    (repo_path / ".aio-fleet.yml").write_text("schema_version: 1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile app manifest")
    repo = RepoConfig(
        name="signoz-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "signoz-aio",
            "image_name": "wgross19/signoz-aio",
            "docker_cache_scope": "signoz-aio-image",
            "pytest_image_tag": "signoz-aio:pytest",
            "publish_profile": "upstream-aio-track",
            "components": {
                "agent": {
                    "release_suffix": "agent",
                    "dockerfile": "Dockerfile",
                    "upstream_config": "upstream.toml",
                }
            },
        },
        defaults={"non_release_paths": [".aio-fleet.yml"]},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "1.0.0-agent.1",
            "target_commitish": release_sha,
        },
    )

    plan = release_plan_for_repo(repo, component="agent")

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def test_release_plan_ignores_test_only_package_noise(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "nanoclaw-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=2.0.64\n")
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.md").write_text("## v2.0.64-aio.6\n")
    (repo_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "helpers.py").write_text("# helpers\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.0.64-aio.6")
    _git(repo_path, "tag", "v2.0.64-aio.6")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    (repo_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-ra'\n"
    )
    (repo_path / "tests" / "helpers.py").write_text("# shared helper shim\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "test(smoke): align pytest helpers")
    repo = RepoConfig(
        name="nanoclaw-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "nanoclaw-aio",
            "image_name": "wgross19/nanoclaw-aio",
            "docker_cache_scope": "nanoclaw-aio-image",
            "pytest_image_tag": "nanoclaw-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={"non_release_paths": ["pyproject.toml", "tests/**"]},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "v2.0.64-aio.6",
            "target_commitish": release_sha,
        },
    )

    plan = release_plan_for_repo(repo)

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def test_release_plan_warns_instead_of_due_for_tagless_history(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=0.7.0-hotfix.3\n")
    (repo_path / "upstream.toml").write_text("")
    (repo_path / "CHANGELOG.md").write_text("## 0.7.0-hotfix.3-aio.2\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.0-hotfix.3-aio.2")
    release_sha = subprocess.check_output(  # nosec B603 B607
        ["git", "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    (repo_path / ".aio-fleet.yml").write_text("schema_version: 1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile app manifest")
    repo = RepoConfig(
        name="sure-aio",
        raw={
            "path": str(repo_path),
            "app_slug": "sure-aio",
            "image_name": "wgross19/sure-aio",
            "docker_cache_scope": "sure-aio-image",
            "pytest_image_tag": "sure-aio:pytest",
            "publish_profile": "upstream-aio-track",
        },
        defaults={"non_release_paths": [".aio-fleet.yml"]},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "0.7.0-hotfix.3-aio.2",
            "target_commitish": release_sha,
        },
    )
    monkeypatch.setattr(release_plan_module, "_safe_latest_aio_tag", lambda _repo: "")
    monkeypatch.setattr(
        release_plan_module,
        "_safe_next_aio",
        lambda _repo: "0.7.0-hotfix.3-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module, "_safe_has_aio_changes", lambda _repo: True
    )

    plan = release_plan_for_repo(repo)

    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "watch"  # nosec B101
    assert plan["warnings"] == [  # nosec B101
        "release tag 0.7.0-hotfix.3-aio.2 is missing locally; "
        "fetch tags before trusting release due"
    ]


def test_release_plan_outputs_component_specific_alpha_publish_action(
    tmp_path: Path, monkeypatch
) -> None:
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
                "aio": {"image_name": "wgross19/sure-aio"},
                "sure-alpha": {
                    "image_name": "wgross19/sure-aio-alpha",
                    "dockerfile": "Dockerfile.alpha",
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    sha = "c" * 40
    monkeypatch.setattr(release_plan_module, "_git_head", lambda _path: sha)
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "sure-alpha/0.7.1-alpha.7-aio.6",
        },
    )
    monkeypatch.setattr(
        release_plan_module,
        "_component_release_tag",
        lambda _repo, _component: "sure-alpha/0.7.1-alpha.7-aio.6",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": (
            "0.7.1-alpha.7-aio.6" if component == "sure-alpha" else "0.7.0-aio.1"
        ),
    )

    def fake_tags(_repo: RepoConfig, *, sha: str, component: str, **_kwargs):
        assert component == "sure-alpha"  # nosec B101
        return SimpleNamespace(
            dockerhub=["wgross19/sure-aio-alpha:latest-alpha"],
            ghcr=["ghcr.io/wgross19/sure-aio-alpha:latest-alpha"],
            all_tags=["wgross19/sure-aio-alpha:latest-alpha"],
        )

    monkeypatch.setattr(release_plan_module, "compute_registry_tags", fake_tags)
    monkeypatch.setattr(
        release_plan_module,
        "verify_registry_tags",
        lambda _tags, **_kwargs: ["wgross19/sure-aio-alpha:latest-alpha: missing"],
    )

    plan = release_plan_for_repo(repo, include_registry=True, component="sure-alpha")

    assert plan["repo"] == "sure-aio"  # nosec B101
    assert plan["component"] == "sure-alpha"  # nosec B101
    assert plan["state"] == "publish-missing"  # nosec B101
    assert plan["next_action"] == (  # nosec B101
        f"uv run aio-fleet release transaction --repo sure-aio "
        f"--component sure-alpha --sha {sha} --dry-run"
    )
    assert plan["operator_commands"]["registry_verify"] == (  # nosec B101
        f"uv run aio-fleet registry verify --repo sure-aio --component sure-alpha --sha {sha} --verbose"
    )
    assert plan["operator_commands"]["release_publish"] == (  # nosec B101
        "uv run aio-fleet release publish --repo sure-aio --component sure-alpha"
    )
    assert plan["operator_commands"]["control_check_publish"] == (  # nosec B101
        f"uv run aio-fleet control-check --repo sure-aio --sha {sha} --event push --publish --publish-component sure-alpha"
    )
    assert plan["operator_commands"]["release_transaction"] == (  # nosec B101
        f"uv run aio-fleet release transaction --repo sure-aio --component sure-alpha --sha {sha} --dry-run"
    )


def test_release_plan_marks_missing_registry_only_prerelease_due(
    tmp_path: Path, monkeypatch
) -> None:
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
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    sha = "e" * 40
    monkeypatch.setattr(release_plan_module, "_git_head", lambda _path: sha)
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "unknown",
            "detail": "release not found",
        },
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_latest_component_tag",
        lambda _repo, _component: "sure-alpha/0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_next_component",
        lambda _repo, _component: "0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_has_registry_only_component_changes",
        lambda _repo, _component, _latest_tag: False,
    )
    monkeypatch.setattr(
        release_plan_module,
        "compute_registry_tags",
        lambda _repo, **_kwargs: SimpleNamespace(
            dockerhub=["wgross19/sure-aio-alpha:latest-alpha"],
            ghcr=["ghcr.io/wgross19/sure-aio-alpha:latest-alpha"],
            all_tags=["wgross19/sure-aio-alpha:latest-alpha"],
        ),
    )
    monkeypatch.setattr(
        release_plan_module, "verify_registry_tags", lambda _tags, **_kwargs: []
    )

    plan = release_plan_for_repo(repo, include_registry=True, component="sure-alpha")

    assert plan["release_due"] is True  # nosec B101
    assert plan["state"] == "release-due"  # nosec B101
    assert plan["next_action"] == (  # nosec B101
        f"uv run aio-fleet release transaction --repo sure-aio "
        f"--component sure-alpha --sha {sha} --dry-run"
    )


def test_release_plan_registry_only_uses_component_tag_for_missing_prerelease_lookup(
    tmp_path: Path, monkeypatch
) -> None:
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
                    "release_policy": "registry_only",
                    "release_history": "github_prerelease",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "release_tag_prefix": "sure-alpha/",
                    "release_suffix": "aio",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module, "_safe_latest_component_tag", lambda _repo, _component: ""
    )
    monkeypatch.setattr(
        release_plan_module,
        "_component_release_tag",
        lambda _repo, _component: "sure-alpha/0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_next_component",
        lambda _repo, _component: "0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_safe_changelog_version",
        lambda _repo, *, component="aio": "0.7.1-alpha.10-aio.1",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_has_registry_only_component_changes",
        lambda _repo, _component, _latest_tag: False,
    )

    lookup_tags: list[str] = []

    def fake_latest_release(_repo, **kwargs):
        lookup_tags.append(str(kwargs.get("tag", "")))
        return {"state": "unknown", "detail": "release not found"}

    monkeypatch.setattr(
        release_plan_module, "_latest_github_release", fake_latest_release
    )

    plan = release_plan_for_repo(repo, component="sure-alpha")

    assert lookup_tags == ["sure-alpha/0.7.1-alpha.10-aio.1"]  # nosec B101
    assert plan["release_due"] is True  # nosec B101


def test_release_plan_keeps_registry_only_helper_out_of_formal_release_lane(
    tmp_path: Path, monkeypatch
) -> None:
    repo = RepoConfig(
        name="nanoclaw-aio",
        raw={
            "path": str(tmp_path),
            "app_slug": "nanoclaw-aio",
            "image_name": "wgross19/nanoclaw-aio",
            "docker_cache_scope": "nanoclaw-aio-image",
            "pytest_image_tag": "nanoclaw-aio:pytest",
            "publish_profile": "multi-component",
            "components": {
                "aio": {"image_name": "wgross19/nanoclaw-aio"},
                "agent": {
                    "image_name": "wgross19/nanoclaw-agent",
                    "dockerfile": "components/nanoclaw-agent/Dockerfile",
                    "release_policy": "registry_only",
                    "release_suffix": "agent",
                    "registry_revision_arg": "AGENT_REVISION",
                },
            },
        },
        defaults={},
        owner="wgross19",
    )
    sha = "d" * 40
    monkeypatch.setattr(release_plan_module, "_git_head", lambda _path: sha)
    monkeypatch.setattr(
        release_plan_module,
        "_safe_latest_component_tag",
        lambda _repo, _component: "v2.0.64-agent.2",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_has_registry_only_component_changes",
        lambda _repo, _component, _latest_tag: False,
    )
    latest_release_calls = []

    def fake_latest_release(_repo: RepoConfig, **_kwargs):
        latest_release_calls.append(_kwargs)
        return {"state": "ok", "tag": "v2.0.64-aio.4"}

    monkeypatch.setattr(
        release_plan_module, "_latest_github_release", fake_latest_release
    )

    plan = release_plan_for_repo(repo, component="agent")

    assert latest_release_calls == []  # nosec B101
    assert plan["latest_release_tag"] == "v2.0.64-agent.2"  # nosec B101
    assert plan["latest_changelog_version"] == "registry-only"  # nosec B101
    assert plan["latest_github_release"] == {  # nosec B101
        "state": "not-applicable",
        "detail": "registry-only component without GitHub release history",
    }
    assert plan["warnings"] == []  # nosec B101
    assert plan["state"] == "current"  # nosec B101
    assert "release_publish" not in plan["operator_commands"]  # nosec B101
    assert plan["operator_commands"]["registry_verify"] == (  # nosec B101
        f"uv run aio-fleet registry verify --repo nanoclaw-aio --component agent --sha {sha} --verbose"
    )


def test_release_plan_manifest_expands_public_components(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {repo_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    publish_profile: upstream-aio-track
    components:
      aio:
        image_name: wgross19/sure-aio
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        release_policy: registry_only
""")

    monkeypatch.setattr(
        release_plan_module,
        "release_plan_for_repo",
        lambda repo, **kwargs: {
            "repo": repo.name,
            "component": kwargs.get("component", "aio"),
            "state": "current",
            "next_action": "none",
        },
    )

    rows = release_plan_for_manifest(load_manifest(manifest_path))

    assert [(row["repo"], row["component"]) for row in rows] == [  # nosec B101
        ("sure-aio", "aio"),
        ("sure-aio", "sure-alpha"),
    ]


def test_release_plan_reports_component_specific_aio_metadata_after_agent_release(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "signoz-aio"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "config", "tag.gpgSign", "false")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "Tests")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_SIGNOZ_VERSION=v0.125.1\n")
    (repo_path / "signoz-aio.xml").write_text("<Container></Container>\n")
    (repo_path / "CHANGELOG.md").write_text(
        "## v0.125.1-aio.1\n\n" "- SigNoz AIO release.\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v0.125.1-aio.1")
    _git(repo_path, "tag", "v0.125.1-aio.1")
    (repo_path / "CHANGELOG.md").write_text(
        "## 0.152.0-agent.2\n\n"
        "- SigNoz agent release.\n\n"
        "## v0.125.1-aio.1\n\n"
        "- SigNoz AIO release.\n"
    )
    (repo_path / "signoz-agent.xml").write_text("<Container></Container>\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.152.0-agent.2")
    (repo_path / ".aio-fleet.yml").write_text("schema_version: 1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile app manifest")
    git = shutil.which("git")
    assert git is not None  # nosec B101
    head = subprocess.check_output(  # nosec B603
        [git, "rev-parse", "HEAD"], cwd=repo_path, text=True
    ).strip()
    repo = RepoConfig(
        name="signoz-aio",
        raw={
            "path": str(repo_path),
            "public": False,
            "app_slug": "signoz-aio",
            "image_name": "wgross19/signoz-aio",
            "docker_cache_scope": "signoz-aio-image",
            "pytest_image_tag": "signoz-aio:pytest",
            "publish_profile": "signoz-suite",
            "components": {
                "aio": {
                    "xml_paths": ["signoz-aio.xml"],
                    "upstream_version_key": "UPSTREAM_SIGNOZ_VERSION",
                    "release_suffix": "aio",
                },
                "agent": {
                    "xml_paths": ["signoz-agent.xml"],
                    "release_suffix": "agent",
                },
            },
        },
        defaults={"non_release_paths": [".aio-fleet.yml"]},
        owner="wgross19",
    )
    monkeypatch.setattr(
        release_plan_module,
        "_latest_github_release",
        lambda _repo, **_kwargs: {
            "state": "ok",
            "tag": "0.152.0-agent.2",
            "target_commitish": head,
        },
    )

    plan = release_plan_for_repo(repo)

    assert plan["component"] == "aio"  # nosec B101
    assert plan["latest_release_tag"] == "v0.125.1-aio.1"  # nosec B101
    assert plan["latest_changelog_version"] == "v0.125.1-aio.1"  # nosec B101
    assert plan["release_due"] is False  # nosec B101
    assert plan["state"] == "current"  # nosec B101


def _git(path: Path, *args: str) -> None:
    git = shutil.which("git")
    assert git is not None  # nosec B101
    result = subprocess.run(  # nosec B603
        [git, *args],
        cwd=path,
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr  # nosec B101
