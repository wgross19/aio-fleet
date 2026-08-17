from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from aio_fleet import smoke
from aio_fleet.manifest import load_manifest


def _manifest(tmp_path: Path) -> Path:
    repo_path = tmp_path / "sure-aio"
    repo_path.mkdir()
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
    publish_platforms: linux/amd64,linux/arm64
    smoke_test:
      env:
        SECRET_KEY_BASE: test-secret
        SELF_HOSTED: "true"
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        publish_platforms: linux/amd64,linux/arm64
        floating_tags:
          - latest-alpha
        smoke_test:
          enabled: true
  amd-only-aio:
    path: {repo_path}
    public: true
    app_slug: amd-only-aio
    image_name: wgross19/amd-only-aio
    docker_cache_scope: amd-only-aio-image
    pytest_image_tag: amd-only-aio:pytest
    publish_platforms: linux/amd64
""")
    return manifest


def test_smoke_targets_respect_arch_and_platforms(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    arm = smoke.smoke_targets(manifest, arch="arm64")
    # amd-only-aio does not publish arm64, so it is skipped for that arch.
    assert {t["repo"] for t in arm} == {"sure-aio"}  # nosec B101
    assert {t["image"] for t in arm} == {  # nosec B101
        "wgross19/sure-aio:latest",
        "wgross19/sure-aio-alpha:latest-alpha",
    }
    amd = smoke.smoke_targets(manifest, arch="amd64")
    assert {t["repo"] for t in amd} == {"sure-aio", "amd-only-aio"}  # nosec B101
    assert {t["image"] for t in amd} == {  # nosec B101
        "wgross19/sure-aio:latest",
        "wgross19/sure-aio-alpha:latest-alpha",
        "wgross19/amd-only-aio:latest",
    }
    sure_env = [t["env"] for t in amd if t["image"] == "wgross19/sure-aio:latest"][0]
    alpha_env = [
        t["env"] for t in amd if t["image"] == "wgross19/sure-aio-alpha:latest-alpha"
    ][0]
    assert sure_env["SECRET_KEY_BASE"] == "test-secret"  # nosec B101
    assert alpha_env["SECRET_KEY_BASE"] == "test-secret"  # nosec B101


def test_smoke_one_passes_on_healthy(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout=600):
        calls.append(cmd)
        if cmd[:2] == ["docker", "pull"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=0, stdout="cid123\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="healthy\n", stderr="")
        if cmd[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(smoke, "_run", fake_run)
    result = smoke.smoke_one(
        {
            "repo": "sure-aio",
            "image": "wgross19/sure-aio:latest",
            "arch": "amd64",
            "env": {"SMOKE_ENV": "1"},
            "start_period": 0,
            "timeout": 10,
        }
    )
    assert result["status"] == "pass"  # nosec B101
    # the container is always cleaned up
    assert ["docker", "rm", "-f", "cid123"] in calls  # nosec B101
    # env is threaded into docker run
    assert any("SMOKE_ENV=1" in part for cmd in calls for part in cmd)  # nosec B101


def test_smoke_one_fails_on_pull_error(monkeypatch) -> None:
    def fake_run(cmd, *, timeout=600):
        if cmd[:2] == ["docker", "pull"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="manifest unknown")
        raise AssertionError(cmd)

    monkeypatch.setattr(smoke, "_run", fake_run)
    result = smoke.smoke_one(
        {
            "repo": "sure-aio",
            "image": "wgross19/sure-aio:latest",
            "arch": "arm64",
            "env": {},
            "start_period": 0,
            "timeout": 10,
        }
    )
    assert result["status"] == "fail"  # nosec B101
    assert "pull failed" in result["detail"]  # nosec B101


def test_smoke_one_accepts_running_without_healthcheck(monkeypatch) -> None:
    def fake_run(cmd, *, timeout=600):
        if cmd[:2] == ["docker", "pull"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0, stdout="no-healthcheck:running\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(smoke, "_run", fake_run)
    result = smoke.smoke_one(
        {
            "repo": "x-aio",
            "image": "wgross19/x-aio:latest",
            "arch": "amd64",
            "env": {},
            "start_period": 0,
            "timeout": 10,
        }
    )
    assert result["status"] == "pass"  # nosec B101
    assert "stayed running" in result["detail"]  # nosec B101


def test_smoke_published_images_writes_report(tmp_path: Path, monkeypatch) -> None:
    manifest = _manifest(tmp_path)
    monkeypatch.setattr(
        smoke,
        "smoke_one",
        lambda target: {
            "repo": target["repo"],
            "image": target["image"],
            "arch": target["arch"],
            "status": "pass",
            "detail": "ok",
        },
    )
    out = tmp_path / "report.json"
    gh = tmp_path / "gh_output.txt"
    report = smoke.smoke_published_images(
        manifest_path=manifest, arch="amd64", output_path=out, github_output=gh
    )
    assert report["status"] == 0  # nosec B101
    assert report["passed"] == report["total"] == 3  # nosec B101
    assert json.loads(out.read_text())["arch"] == "amd64"  # nosec B101
    assert "status=0" in gh.read_text()  # nosec B101
