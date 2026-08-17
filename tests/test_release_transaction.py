from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
from argparse import Namespace
from pathlib import Path

from aio_fleet.cli import cmd_release_preflight, cmd_release_transaction
from aio_fleet.manifest import RepoConfig
from aio_fleet.release_transaction import (
    release_transaction_preflight,
    release_transaction_report,
)


def test_release_transaction_plans_ordered_phases_for_clean_repo(
    tmp_path: Path,
) -> None:
    repo = _repo_config(_init_app_repo(tmp_path / "example-aio"))
    sha = _git(repo.path, "rev-parse", "HEAD").stdout.strip()

    report = release_transaction_report(
        repo,
        components=["aio"],
        expected_sha=sha,
        dry_run=True,
    )

    assert report["status"] in {"ready", "ok"}  # nosec B101
    assert report["expected_sha"] == sha  # nosec B101
    assert [phase["name"] for phase in report["phases"]] == [  # nosec B101
        "refresh-truth",
        "preflight",
        "source-pr",
        "source-checks",
        "source-merge",
        "release-prepare",
        "release-pr",
        "release-checks",
        "release-merge",
        "publish-control-check",
        "registry-verify",
        "github-release",
        "catalog-sync",
        "final-verify",
    ]
    assert report["preflight"]["failure_classes"] == []  # nosec B101


def test_release_transaction_blocks_wrong_head_and_dirty_checkout(
    tmp_path: Path,
) -> None:
    repo = _repo_config(_init_app_repo(tmp_path / "example-aio"))
    expected_sha = _git(repo.path, "rev-parse", "HEAD").stdout.strip()
    (repo.path / "README.md").write_text("dirty\n")
    _git(repo.path, "add", "README.md")
    _git(repo.path, "commit", "-m", "mutate after reviewed sha")

    report = release_transaction_preflight(
        repo,
        components=["aio"],
        expected_sha=expected_sha,
    )

    assert report["status"] == "blocked"  # nosec B101
    assert "checkout-mismatch" in report["failure_classes"]  # nosec B101
    messages = "\n".join(finding["message"] for finding in report["findings"])
    assert "does not match expected" in messages  # nosec B101


def test_release_transaction_blocks_unexpected_uv_lock_drift(
    tmp_path: Path,
) -> None:
    repo = _repo_config(_init_app_repo(tmp_path / "example-aio"))
    (repo.path / "uv.lock").write_text("generated from wrong cwd\n")

    report = release_transaction_preflight(repo, components=["aio"])

    assert report["status"] == "blocked"  # nosec B101
    messages = "\n".join(finding["message"] for finding in report["findings"])
    assert "unexpected uv.lock drift" in messages  # nosec B101


def test_release_transaction_write_requires_explicit_autopilot(
    tmp_path: Path,
) -> None:
    repo = _repo_config(_init_app_repo(tmp_path / "example-aio"))

    report = release_transaction_preflight(repo, components=["aio"], write=True)

    assert report["status"] == "blocked"  # nosec B101
    assert "permission-gap" in report["failure_classes"]  # nosec B101
    assert "required-check-missing" in report["failure_classes"]  # nosec B101
    assert any(  # nosec B101
        "release_transaction.autopilot: true" in finding["message"]
        for finding in report["findings"]
    )


def test_release_transaction_allows_explicit_autopilot_with_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_config(
        _init_app_repo(tmp_path / "example-aio"),
        release_transaction={"autopilot": True},
    )
    for key in [
        "DOCKERHUB_USERNAME",
        "DOCKERHUB_TOKEN",
        "AIO_FLEET_GHCR_TOKEN",
        "DOCKERHUB_DELETE_TOKEN",
    ]:
        monkeypatch.setenv(key, "present")

    report = release_transaction_preflight(
        repo,
        components=["aio"],
        write=True,
        require_credentials=True,
        required_checks_passed=True,
    )

    assert report["status"] == "ok"  # nosec B101
    assert report["failure_classes"] == []  # nosec B101


def test_release_transaction_blocks_forged_autopilot_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _repo_config(
        _init_app_repo(tmp_path / "example-aio"),
        release_transaction={"autopilot": False, "autopilot_explicit": True},
    )
    for key in [
        "DOCKERHUB_USERNAME",
        "DOCKERHUB_TOKEN",
        "AIO_FLEET_GHCR_TOKEN",
        "DOCKERHUB_DELETE_TOKEN",
    ]:
        monkeypatch.setenv(key, "present")

    report = release_transaction_preflight(
        repo,
        components=["aio"],
        write=True,
        require_credentials=True,
        required_checks_passed=True,
    )

    assert report["status"] == "blocked"  # nosec B101
    assert "permission-gap" in report["failure_classes"]  # nosec B101
    assert any(  # nosec B101
        "release_transaction.autopilot: true" in finding["message"]
        for finding in report["findings"]
    )


def test_release_transaction_blocks_pull_request_submodule_policy(
    tmp_path: Path,
) -> None:
    repo = _repo_config(
        _init_app_repo(tmp_path / "mem0-aio"),
        name="mem0-aio",
        checkout_submodules=True,
    )

    report = release_transaction_preflight(
        repo,
        components=["aio"],
        event="pull_request",
    )

    assert report["status"] == "blocked"  # nosec B101
    assert "submodule-policy-mismatch" in report["failure_classes"]  # nosec B101


def test_release_transaction_blocks_unsigned_generated_pr(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _repo_config(_init_app_repo(tmp_path / "example-aio"))
    monkeypatch.setattr(
        "aio_fleet.release_transaction.current_generated_pr_signature_blockers",
        lambda _github_repo, _repo_path: [
            "generated PR #12 has unverified commits: unsigned"
        ],
    )

    report = release_transaction_preflight(repo, components=["aio"])

    assert report["status"] == "blocked"  # nosec B101
    assert "unsigned-generated-pr" in report["failure_classes"]  # nosec B101


def test_release_preflight_cli_outputs_json_failure_classes(
    tmp_path: Path,
    capsys,
) -> None:
    repo_path = _init_app_repo(tmp_path / "example-aio")
    manifest = _write_manifest(tmp_path, repo_path)
    (repo_path / "uv.lock").write_text("generated from wrong cwd\n")

    result = cmd_release_preflight(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            component="aio",
            repo_path=None,
            sha="",
            event="push",
            mode="transaction",
            write=False,
            require_credentials=False,
            required_checks_passed=False,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"  # nosec B101
    assert "checkout-mismatch" in payload["failure_classes"]  # nosec B101


def test_release_transaction_cli_writes_report_json(
    tmp_path: Path,
) -> None:
    repo_path = _init_app_repo(tmp_path / "example-aio")
    manifest = _write_manifest(tmp_path, repo_path)
    report_path = tmp_path / "transaction.json"

    result = cmd_release_transaction(
        Namespace(
            manifest=str(manifest),
            transaction_command="run",
            repo="example-aio",
            component="aio",
            repo_path=None,
            sha="",
            event="push",
            dry_run=True,
            write=False,
            require_credentials=False,
            required_checks_passed=False,
            transaction_id="example-aio-aio-test",
            report_json=str(report_path),
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(report_path.read_text())
    assert payload["transaction_id"] == "example-aio-aio-test"  # nosec B101
    assert payload["dry_run"] is True  # nosec B101


def _repo_config(
    path: Path,
    *,
    name: str = "example-aio",
    checkout_submodules: bool = False,
    release_transaction: dict[str, object] | None = None,
) -> RepoConfig:
    raw: dict[str, object] = {
        "path": str(path),
        "public": True,
        "app_slug": name,
        "image_name": f"wgross19/{name}",
        "docker_cache_scope": f"{name}-image",
        "pytest_image_tag": f"{name}:pytest",
        "publish_profile": "changelog-version",
        "xml_paths": [f"{name}.xml"],
    }
    if checkout_submodules:
        raw["checkout_submodules"] = True
    if release_transaction is not None:
        raw["release_transaction"] = release_transaction
    return RepoConfig(
        name=name,
        raw=raw,
        defaults={"release_transaction": {"publish_policy": "central-control"}},
        owner="wgross19",
    )


def _write_manifest(tmp_path: Path, repo_path: Path) -> Path:
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
    publish_profile: changelog-version
    xml_paths:
      - example-aio.xml
""")
    return manifest


def _init_app_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "fleet@example.invalid")
    _git(path, "config", "user.name", "AIO Fleet")
    (path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.2.3\n")
    (path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.3-aio.1 - 2026-05-19\n\n- release notes\n"
    )
    (path / f"{path.name}.xml").write_text("<Container></Container>\n")
    (path / "README.md").write_text("repo\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial release metadata")
    return path


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
