from __future__ import annotations

import shutil
import subprocess  # nosec B404
from pathlib import Path

from aio_fleet.doctor import fleet_doctor_report
from aio_fleet.manifest import FleetManifest, RepoConfig
from aio_fleet.validators import catalog_asset_failures


def _repo(tmp_path: Path, catalog_assets: list[dict[str, str]]) -> RepoConfig:
    raw = {
        "path": str(tmp_path),
        "app_slug": "mem0-aio",
        "image_name": "wgross19/mem0-aio",
        "docker_cache_scope": "mem0-aio-image",
        "pytest_image_tag": "mem0-aio:pytest",
        "catalog_assets": catalog_assets,
    }
    return RepoConfig(name="mem0-aio", raw=raw, defaults={}, owner="wgross19")


def _manifest(repo_path: Path) -> FleetManifest:
    return FleetManifest(
        path=repo_path / "fleet.yml",
        raw={
            "owner": "wgross19",
            "repos": {
                "example-aio": {
                    "path": str(repo_path),
                    "app_slug": "example-aio",
                    "image_name": "wgross19/example-aio",
                    "docker_cache_scope": "example-aio-image",
                    "pytest_image_tag": "example-aio:pytest",
                }
            },
        },
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    assert git is not None  # nosec B101
    return subprocess.run(  # nosec B603
        [git, *args],
        cwd=path,
        text=True,
        capture_output=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "fleet@example.invalid")
    _git(path, "config", "user.name", "AIO Fleet")
    (path / "README.md").write_text("repo\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")


def test_fleet_doctor_classifies_clean_checkout(tmp_path: Path) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)

    report = fleet_doctor_report(_manifest(repo_path), include_local=True)

    assert report["status"] == "ok"  # nosec B101
    assert report["failure_classes"] == []  # nosec B101


def test_fleet_doctor_classifies_detached_checkout(tmp_path: Path) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)
    head = _git(repo_path, "rev-parse", "HEAD").stdout.strip()
    _git(repo_path, "checkout", "--detach", head)

    report = fleet_doctor_report(_manifest(repo_path), include_local=True)

    assert report["status"] == "failed"  # nosec B101
    assert "detached-checkout" in report["failure_classes"]  # nosec B101


def test_fleet_doctor_classifies_dirty_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)
    (repo_path / "README.md").write_text("dirty\n")

    report = fleet_doctor_report(_manifest(repo_path), include_local=True)

    assert report["status"] == "failed"  # nosec B101
    assert "dirty-repo" in report["failure_classes"]  # nosec B101


def test_fleet_doctor_classifies_stale_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    work = tmp_path / "remote-work"
    repo_path = tmp_path / "example-aio"
    git = shutil.which("git")
    assert git is not None  # nosec B101
    subprocess.run(
        [git, "init", "--bare", origin], check=True, capture_output=True
    )  # nosec B603
    _init_repo(work)
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    subprocess.run(
        [git, "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
    )  # nosec B603
    subprocess.run(
        [git, "clone", "--branch", "main", str(origin), str(repo_path)],
        check=True,
        capture_output=True,
    )  # nosec B603
    _git(repo_path, "config", "user.email", "fleet@example.invalid")
    _git(repo_path, "config", "user.name", "AIO Fleet")
    (work / "README.md").write_text("new remote commit\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "advance remote")
    _git(work, "push")
    _git(repo_path, "fetch", "origin")

    report = fleet_doctor_report(_manifest(repo_path), include_local=True)

    assert report["status"] == "failed"  # nosec B101
    assert "stale-branch" in report["failure_classes"]  # nosec B101


def test_fleet_doctor_classifies_missing_app_checks_permission(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)
    monkeypatch.setattr(
        "aio_fleet.doctor._app_check_targets",
        lambda _repo: [{"source": "main", "event": "push", "sha": "a" * 40}],
    )

    def forbidden(*_args, **kwargs):
        assert kwargs["name"] == "aio-fleet / required permission probe"  # nosec B101
        raise RuntimeError(
            "GitHub check-run request forbidden for wgross19/example-aio"
        )

    monkeypatch.setattr("aio_fleet.doctor.upsert_check_run", forbidden)

    report = fleet_doctor_report(_manifest(repo_path), include_app_checks=True)

    assert report["status"] == "failed"  # nosec B101
    assert "app-check-permission" in report["failure_classes"]  # nosec B101


def test_fleet_doctor_classifies_missing_publish_credentials(tmp_path: Path) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)

    report = fleet_doctor_report(
        _manifest(repo_path),
        include_local=False,
        include_publish=True,
        env={},
    )

    assert report["status"] == "failed"  # nosec B101
    assert "credential-gap" in report["failure_classes"]  # nosec B101


def test_fleet_doctor_classifies_missing_delete_scope_credentials(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "example-aio"
    _init_repo(repo_path)

    report = fleet_doctor_report(
        _manifest(repo_path),
        include_local=False,
        include_cleanup=True,
        check_delete_scope=True,
        env={"DOCKERHUB_USERNAME": "wgross19"},
    )

    assert report["status"] == "failed"  # nosec B101
    assert "delete-scope-gap" in report["failure_classes"]  # nosec B101


def _write_mem0_xml(tmp_path: Path) -> None:
    (tmp_path / "mem0-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/mem0.jpeg</Icon>
</Container>
""")


def test_catalog_asset_check_accepts_matching_icon_source(tmp_path: Path) -> None:
    _write_mem0_xml(tmp_path)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "mem0.jpeg").write_bytes(b"icon")

    failures = catalog_asset_failures(
        _repo(
            tmp_path,
            [
                {"source": "mem0-aio.xml", "target": "mem0-aio.xml"},
                {"source": "assets/mem0.jpeg", "target": "icons/mem0.jpeg"},
            ],
        )
    )

    assert failures == []  # nosec B101


def test_catalog_asset_check_rejects_missing_and_mismatched_icon(
    tmp_path: Path,
) -> None:
    _write_mem0_xml(tmp_path)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "mem0.jpeg").write_bytes(b"icon")

    failures = catalog_asset_failures(
        _repo(
            tmp_path,
            [
                {"source": "mem0-aio.xml", "target": "mem0-aio.xml"},
                {"source": "assets/app-icon.png", "target": "icons/mem0.png"},
            ],
        )
    )

    assert (
        "mem0-aio: catalog_assets source missing: assets/app-icon.png" in failures
    )  # nosec B101
    assert (  # nosec B101
        "mem0-aio: catalog_assets target icons/mem0.png "
        "is not referenced by any catalog XML Icon"
    ) in failures


def test_catalog_asset_check_rejects_unsafe_paths(tmp_path: Path) -> None:
    _write_mem0_xml(tmp_path)

    failures = catalog_asset_failures(
        _repo(
            tmp_path,
            [
                {"source": "../secret.txt", "target": "mem0-aio.xml"},
                {"source": "mem0-aio.xml", "target": ".git/hooks/pre-push"},
            ],
        )
    )

    assert (  # nosec B101
        "mem0-aio: catalog_assets source path is invalid: ../secret.txt" in failures
    )
    assert (  # nosec B101
        "mem0-aio: catalog_assets target path is reserved: .git/hooks/pre-push"
        in failures
    )
