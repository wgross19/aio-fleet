from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aio_fleet import upstream
from aio_fleet.github_writer import BranchCommitResult
from aio_fleet.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_upstream_monitor_detects_version_and_digest_update(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=1.0.0\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:old\n"
        "FROM example/app:${UPSTREAM_VERSION}@${UPSTREAM_IMAGE_DIGEST}\n"
    )
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
    upstream_monitor:
      - component: aio
        name: Example
        source: github-tags
        repo: example/app
        image: example/app
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        digest_key: UPSTREAM_IMAGE_DIGEST
        strategy: pr
""")

    monkeypatch.setattr(
        upstream, "latest_github_tag", lambda *_args, **_kwargs: "1.1.0"
    )
    monkeypatch.setattr(
        upstream, "registry_digest_for_version", lambda *_args, **_kwargs: "sha256:new"
    )

    result = upstream.monitor_repo(load_manifest(manifest).repo("example-aio"))[0]

    assert result.version_update is True  # nosec B101
    assert result.digest_update is True  # nosec B101
    assert result.latest_version == "1.1.0"  # nosec B101
    assert result.latest_digest == "sha256:new"  # nosec B101


def test_github_release_digest_fallback_never_downgrades(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text(
        "ARG UPSTREAM_VERSION=2.0.0\n" "ARG UPSTREAM_IMAGE_DIGEST=sha256:current\n"
    )
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
    upstream_monitor:
      - source: github-releases
        repo: example/app
        image: example/app
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        digest_key: UPSTREAM_IMAGE_DIGEST
        strategy: pr
""")
    candidates = (
        upstream.GitHubReleaseCandidate(tag="v2.1.0", version="2.1.0"),
        upstream.GitHubReleaseCandidate(tag="v1.9.0", version="1.9.0"),
    )

    monkeypatch.setattr(
        upstream,
        "github_release_candidates_result",
        lambda *_args, **_kwargs: (candidates, ()),
    )

    def fake_digest(_image: str, version: str, **_kwargs) -> str:
        if version == "2.1.0":
            raise upstream.RegistryDigestNotFoundError("missing")
        return "sha256:old"

    monkeypatch.setattr(upstream, "registry_digest_for_version", fake_digest)

    result = upstream.monitor_repo(load_manifest(manifest).repo("example-aio"))[0]

    assert result.latest_version == "2.0.0"  # nosec B101
    assert result.latest_digest == "sha256:current"  # nosec B101
    assert result.version_update is False  # nosec B101
    assert result.digest_update is False  # nosec B101
    assert {item["reason"] for item in result.skipped_versions} == {  # nosec B101
        "missing-dockerhub-digest",
        "not-newer-than-current",
    }


def test_github_release_candidates_filter_alpha_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        upstream,
        "http_json",
        lambda _url: [
            {"tag_name": "v0.7.1", "prerelease": False},
            {"tag_name": "v0.7.2-beta.1", "prerelease": True},
            {"tag_name": "v0.7.2-alpha.1", "prerelease": True},
            {"tag_name": "v0.7.1-alpha.7", "prerelease": True},
        ],
    )

    candidates, skipped = upstream.github_release_candidates_result(
        "we-promise/sure",
        stable_only=False,
        prerelease_channel="alpha",
        strip_prefix="v",
    )

    assert [candidate.version for candidate in candidates] == [  # nosec B101
        "0.7.2-alpha.1",
        "0.7.1-alpha.7",
    ]
    assert {item["reason"] for item in skipped} == {  # nosec B101
        "outside-alpha-channel"
    }


def test_shared_version_digest_group_uses_one_resolvable_release(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text(
        "ARG UPSTREAM_DIFY_VERSION=1.9.0\n"
        "ARG UPSTREAM_DIFY_API_DIGEST=sha256:api-old\n"
        "ARG UPSTREAM_DIFY_WEB_DIGEST=sha256:web-old\n"
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  dify-aio:
    path: {repo_path}
    public: true
    app_slug: dify-aio
    image_name: wgross19/dify-aio
    docker_cache_scope: dify-aio-image
    pytest_image_tag: dify-aio:pytest
    upstream_monitor:
      - component: dify-api
        source: github-releases
        repo: langgenius/dify
        image: langgenius/dify-api
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: UPSTREAM_DIFY_VERSION
        digest_key: UPSTREAM_DIFY_API_DIGEST
        strategy: pr
      - component: dify-web
        source: github-releases
        repo: langgenius/dify
        image: langgenius/dify-web
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: UPSTREAM_DIFY_VERSION
        digest_key: UPSTREAM_DIFY_WEB_DIGEST
        strategy: pr
""")
    candidates = (
        upstream.GitHubReleaseCandidate(tag="2.0.0", version="2.0.0"),
        upstream.GitHubReleaseCandidate(tag="1.9.1", version="1.9.1"),
    )

    monkeypatch.setattr(
        upstream,
        "github_release_candidates_result",
        lambda *_args, **_kwargs: (candidates, ()),
    )

    def fake_digest(image: str, version: str, **_kwargs) -> str:
        if image == "langgenius/dify-web" and version == "2.0.0":
            raise upstream.RegistryDigestNotFoundError("missing")
        return f"sha256:{image.rsplit('-', 1)[-1]}-{version}"

    monkeypatch.setattr(upstream, "registry_digest_for_version", fake_digest)

    results = upstream.monitor_repo(
        load_manifest(manifest).repo("dify-aio"), write=True
    )

    assert {result.latest_version for result in results} == {"1.9.1"}  # nosec B101
    text = dockerfile.read_text()
    assert "ARG UPSTREAM_DIFY_VERSION=1.9.1" in text  # nosec B101
    assert "ARG UPSTREAM_DIFY_API_DIGEST=sha256:api-1.9.1" in text  # nosec B101
    assert "ARG UPSTREAM_DIFY_WEB_DIGEST=sha256:web-1.9.1" in text  # nosec B101


def test_upstream_monitor_write_updates_dockerfile(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text(
        "ARG UPSTREAM_VERSION=1.0.0\nARG UPSTREAM_IMAGE_DIGEST=sha256:old\n"
    )
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
    upstream_monitor:
      - source: github-tags
        repo: example/app
        image: example/app
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        digest_key: UPSTREAM_IMAGE_DIGEST
        strategy: pr
""")

    monkeypatch.setattr(
        upstream, "latest_github_tag", lambda *_args, **_kwargs: "1.1.0"
    )
    monkeypatch.setattr(
        upstream, "registry_digest_for_version", lambda *_args, **_kwargs: "sha256:new"
    )

    upstream.monitor_repo(load_manifest(manifest).repo("example-aio"), write=True)

    assert "ARG UPSTREAM_VERSION=1.1.0" in dockerfile.read_text()  # nosec B101
    assert (
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:new" in dockerfile.read_text()
    )  # nosec B101


def test_upstream_monitor_write_moves_commit_pin_with_version(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile"
    old_commit = "a" * 40
    new_commit = "b" * 40
    dockerfile.write_text(
        f"ARG UPSTREAM_VERSION=v1.0.0\nARG UPSTREAM_COMMIT={old_commit}\n"
    )
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
    upstream_monitor:
      - source: github-releases
        repo: example/app
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        commit_key: UPSTREAM_COMMIT
        strategy: pr
""")

    monkeypatch.setattr(
        upstream, "latest_github_release_result", lambda *_a, **_k: ("v1.1.0", ())
    )
    seen: dict[str, str] = {}

    def fake_commit(repo: str, tag: str) -> str:
        seen["repo"] = repo
        seen["tag"] = tag
        return new_commit

    monkeypatch.setattr(upstream, "resolve_github_commit", fake_commit)

    upstream.monitor_repo(load_manifest(manifest).repo("example-aio"), write=True)

    text = dockerfile.read_text()
    # The commit pin moves with the version instead of going stale.
    assert "ARG UPSTREAM_VERSION=v1.1.0" in text  # nosec B101
    assert f"ARG UPSTREAM_COMMIT={new_commit}" in text  # nosec B101
    assert seen == {"repo": "example/app", "tag": "v1.1.0"}  # nosec B101


def test_upstream_monitor_write_updates_alpha_release_history(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile.alpha"
    dockerfile.write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.6\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:old\n"
        "ARG AIO_REVISION=7\n"
    )
    changelog = repo_path / "CHANGELOG.alpha.md"
    changelog.write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.6-aio.7 - 2026-05-20\n\n"
        "### Build\n\n"
        "- Existing alpha release.\n"
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {repo_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    upstream_monitor:
      - component: sure-alpha
        name: Sure Alpha
        source: github-releases
        repo: we-promise/sure
        image: we-promise/sure
        digest_source: ghcr
        dockerfile: Dockerfile.alpha
        version_key: UPSTREAM_VERSION
        version_strip_prefix: v
        digest_key: UPSTREAM_IMAGE_DIGEST
        stable_only: false
        prerelease_channel: alpha
        strategy: pr
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        release_customization_notes:
          - Preserve the Sure AIO alpha import-limit overlay documented in `docs/alpha-lane.md`.
          - Keep `SURE_IMPORT_MAX_NDJSON_SIZE_MB` and `SURE_IMPORT_MAX_ROWS` alpha-only.
          - Keep alpha passkey/WebAuthn template controls separate from stable.
""")

    def fake_http_json(url: str, _headers=None):
        assert "repos/we-promise/sure/releases" in url  # nosec B101
        return [
            {"tag_name": "v0.7.1-alpha.7", "prerelease": True},
            {"tag_name": "v0.7.1-beta.1", "prerelease": True},
        ]

    monkeypatch.setattr(upstream, "http_json", fake_http_json)
    monkeypatch.setattr(
        upstream, "registry_digest_for_version", lambda *_args, **_kwargs: "sha256:new"
    )

    result = upstream.monitor_repo(
        load_manifest(manifest).repo("sure-aio"), write=True
    )[0]

    assert result.latest_version == "0.7.1-alpha.7"  # nosec B101
    text = dockerfile.read_text()
    assert "ARG UPSTREAM_VERSION=0.7.1-alpha.7" in text  # nosec B101
    assert "ARG UPSTREAM_IMAGE_DIGEST=sha256:new" in text  # nosec B101
    assert "ARG AIO_REVISION=1" in text  # nosec B101
    changelog_text = changelog.read_text()
    assert "## 0.7.1-alpha.7-aio.1" in changelog_text  # nosec B101
    assert "### Build\n\n- Track upstream Sure Alpha" in changelog_text  # nosec B101
    assert (
        "### Component Customizations\n\n- Preserve the Sure AIO alpha import-limit"
        in changelog_text
    )  # nosec B101
    assert (  # nosec B101
        "Keep alpha passkey/WebAuthn template controls separate from stable.\n\n"
        "## 0.7.1-alpha.6-aio.7" in changelog_text
    )
    assert "Track upstream Sure Alpha 0.7.1-alpha.7" in changelog_text  # nosec B101
    assert "SURE_IMPORT_MAX_NDJSON_SIZE_MB" in changelog_text  # nosec B101
    assert "passkey/WebAuthn template controls" in changelog_text  # nosec B101


def test_upstream_monitor_write_updates_configured_submodule(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "openmemory").mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text("ARG UPSTREAM_VERSION=v2.0.0\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  mem0-aio:
    path: {repo_path}
    public: true
    app_slug: mem0-aio
    image_name: wgross19/mem0-aio
    docker_cache_scope: mem0-aio-image
    pytest_image_tag: mem0-aio:pytest
    upstream_monitor:
      - source: github-releases
        repo: mem0ai/mem0
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        strategy: pr
        submodule_path: openmemory
        submodule_remote: origin
        submodule_ref_template: codex/openmemory-{{version}}-aio
""")
    calls: list[tuple[Path, list[str]]] = []

    monkeypatch.setattr(
        upstream,
        "latest_github_release_result",
        lambda *_args, **_kwargs: ("v2.0.1", ()),
    )
    monkeypatch.setattr(
        upstream,
        "run_git",
        lambda cwd, args, **_kwargs: calls.append((cwd, args)) or None,
    )

    result = upstream.monitor_repo(
        load_manifest(manifest).repo("mem0-aio"), write=True
    )[0]

    assert "ARG UPSTREAM_VERSION=v2.0.1" in dockerfile.read_text()  # nosec B101
    assert result.submodule_path == "openmemory"  # nosec B101
    assert result.submodule_ref == "codex/openmemory-v2.0.1-aio"  # nosec B101
    assert calls == [  # nosec B101
        (
            repo_path / "openmemory",
            ["fetch", "--tags", "origin", "codex/openmemory-v2.0.1-aio"],
        ),
        (repo_path / "openmemory", ["checkout", "--detach", "FETCH_HEAD"]),
    ]


def test_upstream_monitor_write_updates_multiple_configured_submodules(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    for submodule in ("openmemory", "providers"):
        (repo_path / submodule).mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text(
        "ARG OPENMEMORY_VERSION=v2.0.0\n" "ARG PROVIDERS_VERSION=v1.4.0\n"
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  submodule-aio:
    path: {repo_path}
    public: true
    app_slug: submodule-aio
    image_name: wgross19/submodule-aio
    docker_cache_scope: submodule-aio-image
    pytest_image_tag: submodule-aio:pytest
    upstream_monitor:
      - component: openmemory
        source: github-releases
        repo: mem0ai/mem0
        dockerfile: Dockerfile
        version_key: OPENMEMORY_VERSION
        strategy: pr
        submodule_path: openmemory
        submodule_remote: origin
        submodule_ref_template: codex/openmemory-{{version}}-aio
      - component: providers
        source: github-releases
        repo: example/providers
        dockerfile: Dockerfile
        version_key: PROVIDERS_VERSION
        strategy: pr
        submodule_path: providers
        submodule_remote: upstream
        submodule_ref_template: release/{{version}}
""")
    latest_by_repo = {
        "mem0ai/mem0": "v2.0.1",
        "example/providers": "v1.4.1",
    }
    calls: list[tuple[Path, list[str]]] = []

    monkeypatch.setattr(
        upstream,
        "latest_github_release_result",
        lambda repo, *_args, **_kwargs: (latest_by_repo[repo], ()),
    )
    monkeypatch.setattr(
        upstream,
        "run_git",
        lambda cwd, args, **_kwargs: calls.append((cwd, args)) or None,
    )

    results = upstream.monitor_repo(
        load_manifest(manifest).repo("submodule-aio"), write=True
    )

    assert "ARG OPENMEMORY_VERSION=v2.0.1" in dockerfile.read_text()  # nosec B101
    assert "ARG PROVIDERS_VERSION=v1.4.1" in dockerfile.read_text()  # nosec B101
    assert [result.submodule_path for result in results] == [  # nosec B101
        "openmemory",
        "providers",
    ]
    assert calls == [  # nosec B101
        (
            repo_path / "openmemory",
            ["fetch", "--tags", "origin", "codex/openmemory-v2.0.1-aio"],
        ),
        (repo_path / "openmemory", ["checkout", "--detach", "FETCH_HEAD"]),
        (
            repo_path / "providers",
            ["fetch", "--tags", "upstream", "release/v1.4.1"],
        ),
        (repo_path / "providers", ["checkout", "--detach", "FETCH_HEAD"]),
    ]


def test_upstream_monitor_blocks_missing_configured_submodule_ref(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", remote], check=True)
    submodule = repo_path / "openmemory"
    subprocess.run(["git", "init", submodule], check=True)
    subprocess.run(
        ["git", "-C", submodule, "remote", "add", "origin", remote], check=True
    )
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text("ARG UPSTREAM_VERSION=v2.0.1\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  mem0-aio:
    path: {repo_path}
    public: true
    app_slug: mem0-aio
    image_name: wgross19/mem0-aio
    docker_cache_scope: mem0-aio-image
    pytest_image_tag: mem0-aio:pytest
    upstream_monitor:
      - component: openmemory
        source: github-releases
        repo: mem0ai/mem0
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        strategy: pr
        submodule_path: openmemory
        submodule_remote: origin
        submodule_ref_template: codex/openmemory-{{version}}-aio
""")

    monkeypatch.setattr(
        upstream,
        "latest_github_release_result",
        lambda *_args, **_kwargs: ("v2.0.2", ()),
    )

    result = upstream.monitor_repo(
        load_manifest(manifest).repo("mem0-aio"), write=True
    )[0]
    data = upstream.result_dict(result)

    assert result.blocked is True  # nosec B101
    assert data["state"] == "blocked"  # nosec B101
    assert data["submodule_ref"] == "codex/openmemory-v2.0.2-aio"  # nosec B101
    assert "missing configured submodule ref" in result.blocked_reason  # nosec B101
    assert "ARG UPSTREAM_VERSION=v2.0.1" in dockerfile.read_text()  # nosec B101


def test_upstream_monitor_does_not_write_notify_strategy(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    dockerfile = repo_path / "Dockerfile"
    dockerfile.write_text("ARG UPSTREAM_VERSION=1.0.0\n")
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
    upstream_monitor:
      - source: github-tags
        repo: example/app
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        strategy: notify
""")

    monkeypatch.setattr(
        upstream, "latest_github_tag", lambda *_args, **_kwargs: "1.1.0"
    )

    result = upstream.monitor_repo(
        load_manifest(manifest).repo("example-aio"), write=True
    )

    assert result[0].updates_available is True  # nosec B101
    assert "ARG UPSTREAM_VERSION=1.0.0" in dockerfile.read_text()  # nosec B101


def test_stable_filter_keeps_hotfix_and_excludes_alpha() -> None:
    versions = ["v0.7.0", "v0.7.0-hotfix.1", "v0.7.1-alpha.2"]

    filtered = upstream.filter_versions(versions, stable_only=True)

    assert filtered == ["v0.7.0", "v0.7.0-hotfix.1"]  # nosec B101
    assert sorted(filtered, key=upstream.version_sort_key)[-1] == (  # nosec B101
        "v0.7.0-hotfix.1"
    )


def test_stable_filter_rejects_hotfix_prefixed_prereleases() -> None:
    versions = [
        "v1.2.3",
        "v1.2.4-hotfix-rc.1",
        "v1.2.4-hotfix.alpha",
        "v1.2.4-hotfix.1",
    ]

    filtered = upstream.filter_versions(versions, stable_only=True)

    assert filtered == ["v1.2.3", "v1.2.4-hotfix.1"]  # nosec B101
    assert upstream.is_prerelease_version("v1.2.4-hotfix-rc.1") is True  # nosec B101
    assert upstream.is_prerelease_version("v1.2.4-hotfix.alpha") is True  # nosec B101


def test_github_releases_accept_stable_hotfix_and_report_prerelease_skips(
    monkeypatch,
) -> None:
    def fake_http_json(url: str, _headers=None):
        assert "repos/we-promise/sure/releases" in url  # nosec B101
        return [
            {"tag_name": "v0.7.1-alpha.2", "prerelease": True},
            {"tag_name": "v0.7.1-alpha.1", "prerelease": False},
            {"tag_name": "v0.7.0-hotfix.1", "prerelease": False},
            {"tag_name": "v0.7.0", "prerelease": False},
        ]

    monkeypatch.setattr(upstream, "http_json", fake_http_json)

    latest, skipped = upstream.latest_github_release_result(
        "we-promise/sure", stable_only=True, strip_prefix="v"
    )

    assert latest == "0.7.0-hotfix.1"  # nosec B101
    assert {item["version"]: item["reason"] for item in skipped} == {  # nosec B101
        "0.7.1-alpha.2": "github-prerelease",
        "0.7.1-alpha.1": "version-prerelease",
    }


def test_sure_hotfix_monitor_detects_stable_release_and_digest(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=0.7.0\n"
        "ARG UPSTREAM_IMAGE_DIGEST=sha256:old\n"
        "FROM ghcr.io/we-promise/sure:${UPSTREAM_VERSION}@${UPSTREAM_IMAGE_DIGEST}\n"
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {repo_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    upstream_monitor:
      - component: aio
        name: Sure
        source: github-releases
        repo: we-promise/sure
        image: we-promise/sure
        digest_source: ghcr
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        version_strip_prefix: v
        digest_key: UPSTREAM_IMAGE_DIGEST
        stable_only: true
        strategy: pr
""")

    def fake_http_json(url: str, _headers=None):
        assert "repos/we-promise/sure/releases" in url  # nosec B101
        return [
            {"tag_name": "v0.7.1-alpha.2", "prerelease": True},
            {"tag_name": "v0.7.0-hotfix.1", "prerelease": False},
            {"tag_name": "v0.7.0", "prerelease": False},
        ]

    def fake_digest(
        image: str, version: str, *, registry: str, prefix: str = ""
    ) -> str:
        assert image == "we-promise/sure"  # nosec B101
        assert version == "0.7.0-hotfix.1"  # nosec B101
        assert registry == "ghcr"  # nosec B101
        assert prefix == ""  # nosec B101
        return "sha256:f49fc95b95706fcb7752466edef3c902ba9a746ed6b8ae1206ff22e180ac5006"

    monkeypatch.setattr(upstream, "http_json", fake_http_json)
    monkeypatch.setattr(upstream, "registry_digest_for_version", fake_digest)

    result = upstream.monitor_repo(load_manifest(manifest).repo("sure-aio"))[0]

    assert result.latest_version == "0.7.0-hotfix.1"  # nosec B101
    assert result.version_update is True  # nosec B101
    assert result.latest_digest == (  # nosec B101
        "sha256:f49fc95b95706fcb7752466edef3c902ba9a746ed6b8ae1206ff22e180ac5006"
    )
    assert result.skipped_versions == (  # nosec B101
        {"version": "0.7.1-alpha.2", "reason": "github-prerelease"},
    )


def test_sure_monitor_skips_stable_release_without_published_digest(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
    digest = "sha256:f49fc95b95706fcb7752466edef3c902ba9a746ed6b8ae1206ff22e180ac5006"
    (repo_path / "Dockerfile").write_text(
        "ARG UPSTREAM_VERSION=0.7.0-hotfix.1\n"
        f"ARG UPSTREAM_IMAGE_DIGEST={digest}\n"
        "FROM ghcr.io/we-promise/sure:${UPSTREAM_VERSION}@${UPSTREAM_IMAGE_DIGEST}\n"
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {repo_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    upstream_monitor:
      - component: aio
        name: Sure
        source: github-releases
        repo: we-promise/sure
        image: we-promise/sure
        digest_source: ghcr
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        version_strip_prefix: v
        digest_key: UPSTREAM_IMAGE_DIGEST
        stable_only: true
        strategy: pr
""")

    def fake_http_json(url: str, _headers=None):
        assert "repos/we-promise/sure/releases" in url  # nosec B101
        return [
            {"tag_name": "v0.7.1-alpha.3", "prerelease": True},
            {"tag_name": "v0.7.0-hotfix.2", "prerelease": False},
            {"tag_name": "v0.7.0-hotfix.1", "prerelease": False},
            {"tag_name": "v0.7.0", "prerelease": False},
        ]

    def fake_digest(
        _image: str, version: str, *, registry: str, prefix: str = ""
    ) -> str:
        assert registry == "ghcr"  # nosec B101
        assert prefix == ""  # nosec B101
        if version == "0.7.0-hotfix.2":
            raise upstream.RegistryDigestNotFoundError("missing")
        assert version == "0.7.0-hotfix.1"  # nosec B101
        return digest

    monkeypatch.setattr(upstream, "http_json", fake_http_json)
    monkeypatch.setattr(upstream, "registry_digest_for_version", fake_digest)

    result = upstream.monitor_repo(load_manifest(manifest).repo("sure-aio"))[0]

    assert result.latest_version == "0.7.0-hotfix.1"  # nosec B101
    assert result.version_update is False  # nosec B101
    assert result.digest_update is False  # nosec B101
    assert result.latest_digest == digest  # nosec B101
    assert result.skipped_versions == (  # nosec B101
        {"version": "0.7.0-hotfix.2", "reason": "missing-ghcr-digest"},
        {"version": "0.7.1-alpha.3", "reason": "github-prerelease"},
    )


def test_digest_lookup_tries_unprefixed_hotfix_image_tag(monkeypatch) -> None:
    seen: list[str] = []

    def fake_digest(_image: str, tag: str, *, registry: str) -> str:
        assert registry == "ghcr"  # nosec B101
        seen.append(tag)
        return "sha256:hotfix" if tag == "0.7.0-hotfix.1" else ""

    monkeypatch.setattr(upstream, "registry_digest", fake_digest)

    digest = upstream.registry_digest_for_version(
        "we-promise/sure", "0.7.0-hotfix.1", registry="ghcr"
    )

    assert digest == "sha256:hotfix"  # nosec B101
    assert seen[0] == "0.7.0-hotfix.1"  # nosec B101


def test_create_upstream_pr_skips_notify_only_updates(tmp_path: Path) -> None:
    result = upstream.UpstreamMonitorResult(
        repo="example-aio",
        component="aio",
        name="Example",
        strategy="notify",
        source="github-tags",
        current_version="1.0.0",
        latest_version="1.1.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=Path("Dockerfile"),
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://example.invalid/releases",
    )

    action = upstream.create_or_update_upstream_pr(
        load_manifest(_minimal_manifest(tmp_path)).repo("example-aio"),
        [result],
        dry_run=True,
        post_check=True,
    )

    assert action == {  # nosec B101
        "repo": "example-aio",
        "action": "skipped",
        "reason": "no-pr-strategy-updates",
    }


def test_create_upstream_pr_skips_blocked_updates(tmp_path: Path) -> None:
    result = upstream.UpstreamMonitorResult(
        repo="example-aio",
        component="openmemory",
        name="OpenMemory",
        strategy="pr",
        source="github-releases",
        current_version="v2.0.1",
        latest_version="v2.0.2",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=Path("Dockerfile"),
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://github.com/mem0ai/mem0/releases",
        submodule_path="openmemory",
        submodule_ref="codex/openmemory-v2.0.2-aio",
        blocked_reason="missing configured submodule ref",
        next_action="create and push codex/openmemory-v2.0.2-aio",
    )

    action = upstream.create_or_update_upstream_pr(
        load_manifest(_minimal_manifest(tmp_path)).repo("example-aio"),
        [result],
        dry_run=True,
        post_check=True,
    )

    assert action["action"] == "skipped"  # nosec B101
    assert action["reason"] == "blocked-upstream-update"  # nosec B101
    assert action["blockers"][0]["state"] == "blocked"  # nosec B101


def test_create_upstream_pr_uses_verified_commit_writer(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
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
    result = upstream.UpstreamMonitorResult(
        repo="example-aio",
        component="aio",
        name="Example",
        strategy="pr",
        source="github-tags",
        current_version="1.0.0",
        latest_version="1.1.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=repo_path / "Dockerfile",
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://example.invalid/releases",
    )
    seen: dict[str, object] = {}

    def fake_commit(*_args, **kwargs) -> BranchCommitResult:
        seen.update(kwargs)
        return BranchCommitResult(
            action="committed",
            branch=str(kwargs["branch"]),
            sha="a" * 40,
            method="api",
            verified=True,
            verification={"verified": True, "reason": "valid"},
            committed_paths=list(kwargs["paths"]),
        )

    monkeypatch.setattr(upstream, "commit_paths_to_branch", fake_commit)
    monkeypatch.setattr(upstream, "upsert_pr", lambda *_args, **_kwargs: "https://pr")
    monkeypatch.setattr(
        upstream, "close_superseded_upstream_prs", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(upstream, "upsert_check_run", lambda *_args, **_kwargs: None)

    action = upstream.create_or_update_upstream_pr(
        repo, [result], dry_run=False, post_check=True
    )

    assert seen["branch"] == "codex/upstream-example-aio-1.1.0"  # nosec B101
    assert seen["paths"] == ["Dockerfile"]  # nosec B101
    assert seen["require_verified"] is True  # nosec B101
    assert action["verified"] is True  # nosec B101
    assert action["sha"] == "a" * 40  # nosec B101


def test_create_upstream_pr_includes_alpha_changelog_path(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile.alpha").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {repo_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
""")
    repo = load_manifest(manifest).repo("sure-aio")
    result = upstream.UpstreamMonitorResult(
        repo="sure-aio",
        component="sure-alpha",
        name="Sure Alpha",
        strategy="pr",
        source="github-releases",
        current_version="1.0.0",
        latest_version="1.1.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=repo_path / "Dockerfile.alpha",
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://github.com/we-promise/sure/releases",
    )

    action = upstream.create_or_update_upstream_pr(
        repo, [result], dry_run=True, post_check=False
    )

    assert action["paths"] == [  # nosec B101
        "CHANGELOG.alpha.md",
        "Dockerfile.alpha",
    ]


def test_nanoclaw_upstream_monitor_updates_aio_and_agent_pins() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("nanoclaw-aio")
    configs = upstream.monitor_configs(repo)

    assert [config["component"] for config in configs] == ["aio", "agent"]  # nosec B101
    assert all(
        config["source"] == "github-releases" for config in configs
    )  # nosec B101
    assert all(
        config["repo"] == "nanocoai/nanoclaw" for config in configs
    )  # nosec B101
    assert all(config["stable_only"] is True for config in configs)  # nosec B101


def test_nanoclaw_upstream_pr_commits_both_component_dockerfiles(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    agent_dir = repo_path / "components" / "nanoclaw-agent"
    agent_dir.mkdir(parents=True)
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.63\n")
    (agent_dir / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.63\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  nanoclaw-aio:
    path: {repo_path}
    public: true
    app_slug: nanoclaw-aio
    image_name: wgross19/nanoclaw-aio
    docker_cache_scope: nanoclaw-aio-image
    pytest_image_tag: nanoclaw-aio:pytest
    publish_profile: multi-component
    upstream_commit_paths:
      - Dockerfile
      - components/nanoclaw-agent/Dockerfile
    components:
      aio:
        image_name: wgross19/nanoclaw-aio
        dockerfile: Dockerfile
      agent:
        image_name: wgross19/nanoclaw-agent
        dockerfile: components/nanoclaw-agent/Dockerfile
        context: components/nanoclaw-agent
""")
    repo = load_manifest(manifest).repo("nanoclaw-aio")
    results = [
        upstream.UpstreamMonitorResult(
            repo="nanoclaw-aio",
            component="aio",
            name="NanoClaw",
            strategy="pr",
            source="github-releases",
            current_version="v2.0.63",
            latest_version="v2.0.64",
            current_digest="",
            latest_digest="",
            version_update=True,
            digest_update=False,
            dockerfile=repo_path / "Dockerfile",
            version_key="UPSTREAM_VERSION",
            digest_key="",
            release_notes_url="https://github.com/nanocoai/nanoclaw/releases",
        ),
        upstream.UpstreamMonitorResult(
            repo="nanoclaw-aio",
            component="agent",
            name="NanoClaw Agent",
            strategy="pr",
            source="github-releases",
            current_version="v2.0.63",
            latest_version="v2.0.64",
            current_digest="",
            latest_digest="",
            version_update=True,
            digest_update=False,
            dockerfile=agent_dir / "Dockerfile",
            version_key="UPSTREAM_VERSION",
            digest_key="",
            release_notes_url="https://github.com/nanocoai/nanoclaw/releases",
        ),
    ]

    action = upstream.create_or_update_upstream_pr(
        repo, results, dry_run=True, post_check=False
    )

    assert action["paths"] == [  # nosec B101
        "Dockerfile",
        "components/nanoclaw-agent/Dockerfile",
    ]


def test_upstream_body_mentions_source_first_catalog_sync(tmp_path: Path) -> None:
    repo = load_manifest(_minimal_manifest(tmp_path)).repo("example-aio")
    result = upstream.UpstreamMonitorResult(
        repo="example-aio",
        component="aio",
        name="Example",
        strategy="pr",
        source="github-tags",
        current_version="1.0.0",
        latest_version="1.1.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=repo.path / "Dockerfile",
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://example.invalid/releases",
    )

    body = upstream.upstream_body(repo, [result])

    assert "catalog sync follows the validated source repo" in body  # nosec B101
    assert "Release notes: https://example.invalid/releases" in body  # nosec B101


def test_upstream_body_rejects_non_public_changed_paths(tmp_path: Path) -> None:
    repo = load_manifest(_minimal_manifest(tmp_path)).repo("example-aio")
    result = upstream.UpstreamMonitorResult(
        repo="example-aio",
        component="aio",
        name="Example",
        strategy="pr",
        source="github-tags",
        current_version="1.0.0",
        latest_version="1.1.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=repo.path / "Dockerfile",
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="https://example.invalid/releases",
    )

    with pytest.raises(ValueError, match="upstream PR body"):
        upstream.upstream_body(
            repo,
            [result],
            changed_paths=["/Users/shadowbook/Documents/example-aio/Dockerfile"],
        )


def test_github_token_uses_app_token_without_standard_gh_env(monkeypatch) -> None:
    upstream.github_token.cache_clear()
    for env_name in (
        "AIO_FLEET_UPSTREAM_TOKEN",
        "APP_TOKEN",
        "AIO_FLEET_CHECK_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("APP_TOKEN", "app-token")

    assert upstream.github_token() == "app-token"  # nosec B101
    upstream.github_token.cache_clear()


def test_github_cli_env_exposes_only_gh_token(monkeypatch) -> None:
    upstream.github_token.cache_clear()
    monkeypatch.setenv("APP_TOKEN", "app-token")
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")

    env = upstream.github_cli_env()

    assert env is not None  # nosec B101
    assert env["GH_TOKEN"] == "app-token"  # nosec B101
    assert "GITHUB_TOKEN" not in env  # nosec B101
    upstream.github_token.cache_clear()


def _minimal_manifest(repo_path: Path) -> Path:
    manifest = repo_path / "fleet.yml"
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
