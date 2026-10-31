from __future__ import annotations

from pathlib import Path

from aio_fleet.floors import (
    dependency_floor_violations,
    parse_floor,
    satisfies,
    satisfies_numbers,
    version_numbers,
)
from aio_fleet.manifest import RepoConfig, load_manifest
from aio_fleet.upstream import (
    GitHubReleaseCandidate,
    UpstreamMonitorResult,
    apply_dependency_floors,
    create_or_update_upstream_pr,
)

FLEET = """\
schema_version: 1
owner: wgross19
repos:
  gbrain-aio:
    public: true
    path: {app_dir}
    github_repo: wgross19/gbrain-aio
    image_name: dub19/gbrain-aio
    docker_cache_scope: gbrain-aio-image
    pytest_image_tag: gbrain-aio:pytest
    app_slug: gbrain-aio
    upstream_monitor:
      - component: gbrain
        name: GBrain
        source: github-tags
        repo: garrytan/gbrain
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        commit_key: GBRAIN_GIT_SHA
        stable_only: true
        strategy: pr
      - component: bun
        name: Bun
        source: github-releases
        repo: oven-sh/bun
        image: oven/bun
        digest_source: dockerhub
        dockerfile: Dockerfile
        version_key: BUN_VERSION
        digest_key: BUN_DIGEST
        version_strip_prefix: bun-v
        stable_only: true
        strategy: notify
        floor:
          from_component: gbrain
          file: package.json
          field: engines.bun
"""

DOCKERFILE = """\
ARG BUN_VERSION={bun}
ARG BUN_DIGEST={digest}
ARG CADDY_VERSION=2.10.2
ARG GBRAIN_GIT_SHA={gbrain}
ARG UPSTREAM_VERSION={upstream}
"""

GBRAIN_SHA = "fd22a75d136df59bd7d3d43f6ccd6b9ef823037a"


def _write_repo(
    tmp_path: Path, *, bun: str = "1.4.2", gbrain: str = GBRAIN_SHA
) -> RepoConfig:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "Dockerfile").write_text(
        DOCKERFILE.format(bun=bun, gbrain=gbrain, digest="sha256:9114c058", upstream="v0.60.113.0")
    )
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text(FLEET.format(app_dir=app_dir))
    return load_manifest(manifest_path).repo("gbrain-aio")


def _gbrain_result(repo: RepoConfig, *, latest_commit: str = "deadbeefcafe") -> UpstreamMonitorResult:
    return UpstreamMonitorResult(
        repo="gbrain-aio",
        component="gbrain",
        name="GBrain",
        strategy="pr",
        source="github-tags",
        current_version="v0.60.113.0",
        latest_version="v0.60.492.0",
        current_digest="",
        latest_digest="",
        version_update=True,
        digest_update=False,
        dockerfile=repo.path / "Dockerfile",
        version_key="UPSTREAM_VERSION",
        digest_key="",
        release_notes_url="",
        commit_key="GBRAIN_GIT_SHA",
        current_commit=GBRAIN_SHA,
        latest_commit=latest_commit,
    )


def test_parse_floor_accepts_operators_and_bare_versions() -> None:
    spec = parse_floor(">=1.4.4")
    assert spec is not None  # nosec B101
    assert spec.op == ">="  # nosec B101
    assert spec.numbers == (1, 4, 4)  # nosec B101
    bare = parse_floor("v1.4.4")
    assert bare is not None and bare.op == ">="  # nosec B101
    assert parse_floor(">= v1.4.4") is not None  # nosec B101
    assert parse_floor("") is None  # nosec B101
    assert parse_floor("latest") is None  # nosec B101
    assert parse_floor(None) is None  # nosec B101


def test_version_numbers_and_satisfies() -> None:
    assert version_numbers("1.4.2") == (1, 4, 2)  # nosec B101
    assert version_numbers("v1.4.2") == (1, 4, 2)  # nosec B101
    assert version_numbers("canary") is None  # nosec B101
    spec = parse_floor(">=1.4.4")
    assert spec is not None  # nosec B101
    assert satisfies_numbers((1, 4, 4), spec)  # nosec B101
    assert satisfies_numbers((1, 5, 0), spec)  # nosec B101
    assert not satisfies_numbers((1, 4, 2), spec)  # nosec B101
    assert satisfies("1.4.4", spec) is True  # nosec B101
    assert satisfies("canary", spec) is None  # nosec B101


def test_floor_violation_detected_at_pinned_provider(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    violations = dependency_floor_violations(
        repo,
        repo.path,  # type: ignore[attr-defined]
        fetch_json=lambda url: {"engines": {"bun": ">=1.4.4"}},
    )
    assert len(violations) == 1  # nosec B101
    record = violations[0]
    assert record["component"] == "bun"  # nosec B101
    assert record["spec"] == ">=1.4.4"  # nosec B101
    assert record["pinned"] == "1.4.2"  # nosec B101
    assert record["provider_ref"] == GBRAIN_SHA  # nosec B101
    assert record["provider_repo"] == "garrytan/gbrain"  # nosec B101
    assert record["status"] == "violation"  # nosec B101
    assert ">=1.4.4" in record["hint"]  # nosec B101


def test_floor_ok_when_pinned_satisfies(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path, bun="1.4.4")
    violations = dependency_floor_violations(
        repo,
        repo.path,  # type: ignore[attr-defined]
        fetch_json=lambda url: {"engines": {"bun": ">=1.4.4"}},
    )
    assert violations == []  # nosec B101


def test_floor_fetch_failure_is_skipped_not_failed(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    violations = dependency_floor_violations(
        repo,
        repo.path,  # type: ignore[attr-defined]
        fetch_json=lambda url: None,
    )
    assert violations == []  # nosec B101


def test_apply_dependency_floors_bumps_bun_in_pr(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    state = apply_dependency_floors(
        repo,
        [_gbrain_result(repo)],
        fetch_json=lambda url: {"engines": {"bun": ">=1.4.4"}},
        candidates_resolver=lambda config: [
            GitHubReleaseCandidate(tag="bun-v1.4.4", version="1.4.4")
        ],
        digest_resolver=lambda config, version: "sha256:1d22548f",
    )
    assert state["unresolvable"] == []  # nosec B101
    assert len(state["bumps"]) == 1  # nosec B101
    assert state["bumps"][0]["new_version"] == "1.4.4"  # nosec B101
    assert state["changed_dockerfiles_rel"] == ["Dockerfile"]  # nosec B101
    text = (repo.path / "Dockerfile").read_text()  # type: ignore[attr-defined]
    assert "ARG BUN_VERSION=1.4.4" in text  # nosec B101
    assert "ARG BUN_DIGEST=sha256:1d22548f" in text  # nosec B101


def test_apply_dependency_floors_dry_run_touches_nothing(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    state = apply_dependency_floors(
        repo,
        [_gbrain_result(repo)],
        dry_run=True,
        fetch_json=lambda url: {"engines": {"bun": ">=1.4.4"}},
        candidates_resolver=lambda config: [
            GitHubReleaseCandidate(tag="bun-v1.4.4", version="1.4.4")
        ],
        digest_resolver=lambda config, version: "sha256:1d22548f",
    )
    assert state["bumps"][0]["action"] == "would-bump"  # nosec B101
    assert state["changed_dockerfiles_rel"] == []  # nosec B101
    text = (repo.path / "Dockerfile").read_text()  # type: ignore[attr-defined]
    assert "ARG BUN_VERSION=1.4.2" in text  # nosec B101


def test_apply_dependency_floors_uses_live_resolvers(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _write_repo(tmp_path)
    monkeypatch.setattr(
        "aio_fleet.floors.load_floor_json",
        lambda url: {"engines": {"bun": ">=1.4.4"}},
    )
    monkeypatch.setattr(
        "aio_fleet.upstream.github_release_candidates_result",
        lambda repo, **kwargs: ([GitHubReleaseCandidate(tag="bun-v1.4.4", version="1.4.4")], []),
    )
    monkeypatch.setattr(
        "aio_fleet.upstream.registry_digest_for_version",
        lambda image, version, *, registry, prefix="": "sha256:live",
    )
    state = apply_dependency_floors(repo, [_gbrain_result(repo)])
    assert state["unresolvable"] == []  # nosec B101
    assert state["bumps"][0]["new_version"] == "1.4.4"  # nosec B101
    text = (repo.path / "Dockerfile").read_text()  # type: ignore[attr-defined]
    assert "ARG BUN_VERSION=1.4.4" in text  # nosec B101
    assert "ARG BUN_DIGEST=sha256:live" in text  # nosec B101


def test_create_blocks_when_floor_unresolvable(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _write_repo(tmp_path)
    monkeypatch.setattr(
        "aio_fleet.floors.load_floor_json",
        lambda url: {"engines": {"bun": ">=9.9.9"}},
    )
    monkeypatch.setattr(
        "aio_fleet.upstream.github_release_candidates_result",
        lambda repo, **kwargs: ([GitHubReleaseCandidate(tag="bun-v1.4.2", version="1.4.2")], []),
    )
    action = create_or_update_upstream_pr(
        repo,
        [_gbrain_result(repo)],
        dry_run=False,
        post_check=False,
    )
    assert action["action"] == "skipped"  # nosec B101
    assert action["reason"] == "blocked-dependency-floor"  # nosec B101
    assert action["blockers"][0]["component"] == "bun"  # nosec B101
    assert action["blockers"][0]["floor"] == ">=9.9.9"  # nosec B101