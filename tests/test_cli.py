from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aio_fleet import cli
from aio_fleet.change_scope import CHECK_MODE_FAST_CLEANUP, CHECK_MODE_FULL
from aio_fleet.cli import (
    cmd_alert_doctor,
    cmd_alert_send,
    cmd_alert_test,
    cmd_check_run,
    cmd_control_check,
    cmd_debt_report,
    cmd_doctor,
    cmd_export_app_manifest,
    cmd_fleet_dashboard_commands,
    cmd_fleet_dashboard_update,
    cmd_fleet_queue_dispatch,
    cmd_fleet_queue_generate,
    cmd_fleet_report_closeout,
    cmd_fleet_report_explain_run,
    cmd_fleet_report_generate,
    cmd_fleet_report_schema,
    cmd_fleet_report_validate,
    cmd_fleetbot_render_command,
    cmd_hooks_install,
    cmd_infra_doctor,
    cmd_onboard_repo,
    cmd_poll,
    cmd_promote_rehab,
    cmd_registry_delete_dockerhub_tags,
    cmd_registry_preflight,
    cmd_registry_publish,
    cmd_registry_verify,
    cmd_release_plan,
    cmd_release_publish,
    cmd_release_publish_github_prereleases,
    cmd_release_readiness,
    cmd_release_reconcile,
    cmd_security_audit_workflows,
    cmd_signing_doctor,
    cmd_standards_reconcile,
    cmd_trunk_audit,
    cmd_trunk_run,
    cmd_upstream_assess,
    cmd_upstream_monitor,
    cmd_validate,
    cmd_validate_template_common,
    cmd_workflow_control_report,
)
from aio_fleet.hooks import run_local_trunk_overlay
from aio_fleet.manifest import load_manifest
from aio_fleet.poll import PollTarget
from aio_fleet.registry import RegistryTagSet


def test_trunk_audit_summarizes_repo_results(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".trunk").mkdir(parents=True)
    (repo_path / ".trunk" / "trunk.yaml").write_text("version: 0.1\n")
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

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        assert command[:2] == ["trunk", "check"]  # nosec B101
        assert cwd == repo_path  # nosec B101
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_trunk_audit(
        Namespace(manifest=str(manifest), repo=None, verbose=False)
    )

    assert result == 0  # nosec B101
    assert "example-aio: trunk=ok" in capsys.readouterr().out  # nosec B101


def test_trunk_run_local_uses_checkout_overlay(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    called = {}

    def fake_local(
        repo, *, fix: bool = False, all_files: bool = True
    ) -> SimpleNamespace:
        called["repo"] = repo.name
        called["path"] = repo.path
        called["fix"] = fix
        called["all_files"] = all_files
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "run_local_trunk_overlay", fake_local)

    result = cmd_trunk_run(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            all=False,
            fix=True,
            local=True,
            all_files=False,
        )
    )

    assert result == 0  # nosec B101
    assert called == {
        "repo": "example-aio",
        "path": repo_path,
        "fix": True,
        "all_files": False,
    }  # nosec B101
    assert "example-aio: trunk=ok" in capsys.readouterr().out  # nosec B101


def test_trunk_run_local_accepts_repo_path_without_manifest_repo(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    catalog_path = tmp_path / "awesome-unraid"
    catalog_path.mkdir()
    called = {}

    def fake_local(
        repo, *, fix: bool = False, all_files: bool = True
    ) -> SimpleNamespace:
        called["repo"] = repo.name
        called["path"] = repo.path
        called["fix"] = fix
        called["all_files"] = all_files
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "run_local_trunk_overlay", fake_local)

    result = cmd_trunk_run(
        Namespace(
            manifest=str(manifest),
            repo=None,
            repo_path=str(catalog_path),
            all=False,
            fix=False,
            local=True,
            all_files=True,
        )
    )

    assert result == 0  # nosec B101
    assert called == {  # nosec B101
        "repo": "awesome-unraid",
        "path": catalog_path,
        "fix": False,
        "all_files": True,
    }
    assert "awesome-unraid: trunk=ok" in capsys.readouterr().out  # nosec B101


def test_run_local_trunk_overlay_cleans_temporary_config(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    fake_trunk = tmp_path / "trunk"
    fake_trunk.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_trunk.chmod(0o755)
    monkeypatch.setenv("TRUNK_PATH", str(fake_trunk))

    result = run_local_trunk_overlay(load_manifest(manifest).repo("example-aio"))

    assert result.returncode == 0  # nosec B101
    assert not (repo_path / ".trunk").exists()  # nosec B101


def test_run_local_trunk_overlay_uses_central_config_when_repo_trunk_exists(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    repo_trunk = repo_path / ".trunk"
    repo_trunk.mkdir()
    existing = repo_trunk / "runtime-state"
    existing.write_text("keep\n")
    attacker_config = repo_trunk / "trunk.yaml"
    attacker_config.write_text("attacker-owned\n")
    fake_trunk = tmp_path / "trunk"
    fake_trunk.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "config = Path('.trunk/trunk.yaml').read_text()\n"
        "if 'attacker-owned' in config:\n"
        "    sys.exit(5)\n"
        "if 'version: 0.1' not in config:\n"
        "    sys.exit(6)\n"
        "Path('.trunk/out').mkdir(parents=True, exist_ok=True)\n"
        "Path('.trunk/out/generated').write_text('scratch')\n"
    )
    fake_trunk.chmod(0o755)
    monkeypatch.setenv("TRUNK_PATH", str(fake_trunk))

    result = run_local_trunk_overlay(load_manifest(manifest).repo("example-aio"))

    assert result.returncode == 0  # nosec B101
    assert existing.read_text() == "keep\n"  # nosec B101
    assert attacker_config.read_text() == "attacker-owned\n"  # nosec B101
    assert not (repo_trunk / "out" / "generated").exists()  # nosec B101


def test_run_local_trunk_overlay_ignores_temporary_trunk_backup(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    repo_trunk = repo_path / ".trunk"
    repo_trunk.mkdir()
    existing = repo_trunk / "runtime-state"
    existing.write_text("keep\n")
    fake_trunk = tmp_path / "trunk"
    fake_trunk.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "backup_dirs = [\n"
        "    path.name\n"
        "    for path in Path('.').glob('.trunk.aio-fleet-backup-*')\n"
        "    if path.is_dir()\n"
        "]\n"
        "if not backup_dirs:\n"
        "    sys.exit(4)\n"
        "ignore_args = [arg for arg in sys.argv if arg.startswith('--ignore=')]\n"
        "expected = f'--ignore={backup_dirs[0]}/**'\n"
        "if expected not in ignore_args:\n"
        "    sys.exit(3)\n"
    )
    fake_trunk.chmod(0o755)
    monkeypatch.setenv("TRUNK_PATH", str(fake_trunk))

    result = run_local_trunk_overlay(load_manifest(manifest).repo("example-aio"))

    assert result.returncode == 0  # nosec B101
    assert existing.read_text() == "keep\n"  # nosec B101
    assert not list(repo_path.glob(".trunk.aio-fleet-backup-*"))  # nosec B101


def test_run_local_trunk_overlay_restores_existing_trunk_on_copy_error(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    repo_trunk = repo_path / ".trunk"
    repo_trunk.mkdir()
    existing = repo_trunk / "runtime-state"
    existing.write_text("keep\n")
    fake_trunk = tmp_path / "trunk"
    fake_trunk.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_trunk.chmod(0o755)
    monkeypatch.setenv("TRUNK_PATH", str(fake_trunk))

    def fail_copy(_central_trunk: Path, target_trunk: Path) -> None:
        target_trunk.mkdir()
        target_trunk.joinpath("partial").write_text("scratch\n")
        raise RuntimeError("copy failed")

    monkeypatch.setattr("aio_fleet.hooks.copy_trunk_overlay", fail_copy)

    try:
        run_local_trunk_overlay(load_manifest(manifest).repo("example-aio"))
    except RuntimeError as exc:
        assert str(exc) == "copy failed"  # nosec B101
    else:
        raise AssertionError("expected overlay copy failure")

    assert existing.read_text() == "keep\n"  # nosec B101
    assert not (repo_trunk / "partial").exists()  # nosec B101
    assert not list(repo_path.glob(".trunk.aio-fleet-backup-*"))  # nosec B101


def test_run_local_trunk_overlay_strips_hook_actions_and_restores_hooks_path(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    subprocess.run(  # nosec B603 B607
        ["git", "init"], cwd=repo_path, check=True, capture_output=True
    )
    subprocess.run(  # nosec B603 B607
        ["git", "config", "core.hooksPath", ".git/aio-fleet-hooks"],
        cwd=repo_path,
        check=True,
    )
    fake_trunk = tmp_path / "trunk"
    fake_trunk.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "if '--ignore=.trunk/**' not in sys.argv:\n"
        "    sys.exit(3)\n"
        "config = Path('.trunk/trunk.yaml').read_text()\n"
        "if 'trunk-check-pre-push' in config or '\\nactions:' in config:\n"
        "    sys.exit(2)\n"
        "subprocess.run(\n"
        "    ['git', 'config', 'core.hooksPath', '/tmp/trunk-owned-hooks'],\n"
        "    check=True,\n"
        ")\n"
    )
    fake_trunk.chmod(0o755)
    monkeypatch.setenv("TRUNK_PATH", str(fake_trunk))

    result = run_local_trunk_overlay(load_manifest(manifest).repo("example-aio"))

    hooks_path = subprocess.run(  # nosec B603 B607
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert result.returncode == 0  # nosec B101
    assert hooks_path == ".git/aio-fleet-hooks"  # nosec B101
    assert not (repo_path / ".trunk").exists()  # nosec B101


def test_hooks_install_writes_local_hooks(tmp_path: Path) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    subprocess.run(  # nosec B603 B607
        ["git", "init"], cwd=repo_path, check=True, capture_output=True
    )

    result = cmd_hooks_install(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            all=False,
            include_destinations=False,
        )
    )

    assert result == 0  # nosec B101
    hooks_path = subprocess.run(  # nosec B603 B607
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    pre_commit = Path(hooks_path) / "pre-commit"
    pre_push = Path(hooks_path) / "pre-push"

    assert pre_commit.exists()  # nosec B101
    assert pre_push.exists()  # nosec B101
    assert "--local --changed --fix" in pre_commit.read_text()  # nosec B101
    assert "--local --changed --no-fix" in pre_push.read_text()  # nosec B101
    assert "validate-repo --repo" in pre_push.read_text()  # nosec B101


def test_hooks_install_can_include_dashboard_destinations(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    catalog_path = tmp_path / "awesome-unraid"
    repo_path.mkdir()
    catalog_path.mkdir()
    subprocess.run(  # nosec B603 B607
        ["git", "init"], cwd=repo_path, check=True, capture_output=True
    )
    subprocess.run(  # nosec B603 B607
        ["git", "init"], cwd=catalog_path, check=True, capture_output=True
    )
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
repos:
  example-aio:
    path: {repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")

    result = cmd_hooks_install(
        Namespace(
            manifest=str(manifest),
            repo=None,
            repo_path=None,
            all=True,
            include_destinations=True,
        )
    )

    assert result == 0  # nosec B101
    catalog_hooks = subprocess.run(  # nosec B603 B607
        ["git", "config", "--get", "core.hooksPath"],
        cwd=catalog_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    pre_push = Path(catalog_hooks) / "pre-push"
    assert pre_push.exists()  # nosec B101
    assert "validate-catalog --catalog-path" in pre_push.read_text()  # nosec B101
    assert "validate-repo --repo" not in pre_push.read_text()  # nosec B101


def test_signing_doctor_cli_outputs_json_summary(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setattr(
        cli,
        "signing_doctor_report",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "failure_classes": [],
            "summary": {"checks": 1, "failed": 0, "warnings": 0},
            "checks": [
                {
                    "name": "fleetbot-credentials",
                    "status": "ok",
                    "class": "ok",
                    "detail": "Fleetbot GitHub App credentials are present",
                }
            ],
        },
    )

    result = cmd_signing_doctor(
        Namespace(
            manifest=str(manifest),
            repo=None,
            all=False,
            no_hooks=True,
            format="json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert payload["status"] == "ok"  # nosec B101


def test_debt_report_outputs_json_summary(tmp_path: Path, monkeypatch, capsys) -> None:
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
""")

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "rev-list"]:
            return SimpleNamespace(returncode=0, stdout="0 0\n", stderr="")
        if command[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_debt_report(
        Namespace(
            manifest=str(manifest),
            catalog_path=None,
            github=False,
            policy="unused.yml",
            trunk=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    assert '"repos": 1' in capsys.readouterr().out  # nosec B101


def test_standards_reconcile_reports_manifest_and_cleanup_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    (repo_path / ".trunk" / "actions").mkdir(parents=True)

    monkeypatch.setattr(cli, "release_plan_rows_for_repo", lambda *_args, **_kwargs: [])

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=True,
            registry=False,
            write=False,
            allow_drift=True,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["status"] == "actionable"  # nosec B101
    assert report["summary"]["by_class"] == {  # nosec B101
        "manifest-drift": 1,
        "retired-shared-path": 1,
    }
    assert {action["kind"] for action in report["actions"]} == {  # nosec B101
        "app-manifest",
        "cleanup",
    }


def test_standards_reconcile_marks_untracked_cleanup_as_local_only(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    (repo_path / "Dockerfile").write_text("FROM scratch\n")
    _git(repo_path, "add", "Dockerfile")
    _git(repo_path, "commit", "-m", "chore(test): init")
    (repo_path / ".trunk" / "actions").mkdir(parents=True)

    monkeypatch.setattr(cli, "_standards_manifest_actions", lambda *_args: [])
    monkeypatch.setattr(cli, "release_plan_rows_for_repo", lambda *_args, **_kwargs: [])

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=True,
            registry=False,
            write=False,
            allow_drift=False,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["status"] == "ok"  # nosec B101
    assert report["summary"]["actionable"] == 0  # nosec B101
    assert report["actions"][0]["severity"] == "info"  # nosec B101
    assert report["actions"][0]["provenance"] == "local-only"  # nosec B101


def test_standards_reconcile_reports_public_catalog_destination_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    catalog_path = tmp_path / "awesome-unraid"
    private_path = tmp_path / "private-catalog"
    catalog_path.mkdir()
    private_path.mkdir()
    _git(catalog_path, "init")
    _git(catalog_path, "config", "user.email", "tests@example.invalid")
    _git(catalog_path, "config", "user.name", "aio-fleet tests")
    _git(catalog_path, "config", "commit.gpgsign", "false")
    (catalog_path / "scripts").mkdir()
    (catalog_path / "scripts" / "validate-readme-inventory.py").write_text("")
    _git(catalog_path, "add", ".")
    _git(catalog_path, "commit", "-m", "chore(test): init catalog")
    (private_path / "cliff.toml").write_text("[changelog]\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
      public: true
    private-catalog:
      path: {private_path}
      public: false
repos:
  example-aio:
    path: {repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")

    monkeypatch.setattr(cli, "_standards_manifest_actions", lambda *_args: [])
    monkeypatch.setattr(cli, "cleanup_findings", lambda *_args: [])
    monkeypatch.setattr(cli, "resolve_aio_fleet_ref", lambda *_args: "1" * 40)

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=False,
            registry=False,
            write=False,
            allow_drift=True,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["status"] == "actionable"  # nosec B101
    assert report["summary"]["repos"] == 2  # nosec B101
    assert {
        (action["repo"], action["kind"], action["class"])
        for action in report["actions"]
    } == {  # nosec B101
        ("awesome-unraid", "catalog-cleanup", "retired-catalog-path"),
        ("awesome-unraid", "catalog-workflow", "catalog-workflow-drift"),
    }
    workflow_action = next(
        action for action in report["actions"] if action["kind"] == "catalog-workflow"
    )
    assert f"--catalog-path {catalog_path}" in workflow_action["command"]  # nosec B101
    assert "private-catalog" not in json.dumps(report)  # nosec B101


def test_standards_reconcile_skips_ref_lookup_without_public_destinations(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setattr(cli, "_standards_manifest_actions", lambda *_args: [])
    monkeypatch.setattr(cli, "cleanup_findings", lambda *_args: [])
    monkeypatch.setattr(
        cli,
        "resolve_aio_fleet_ref",
        lambda *_args: pytest.fail("ref lookup should not run without destinations"),
    )

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=False,
            registry=False,
            write=False,
            allow_drift=False,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["status"] == "ok"  # nosec B101


def test_standards_reconcile_reports_missing_release_checkout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    missing_repo_path = tmp_path / "missing-repo"
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {missing_repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")

    monkeypatch.setattr(cli, "_standards_manifest_actions", lambda *_args: [])
    monkeypatch.setattr(cli, "cleanup_findings", lambda *_args: [])
    monkeypatch.setattr(
        cli,
        "release_plan_rows_for_repo",
        lambda *_args, **_kwargs: pytest.fail("missing checkouts must not be planned"),
    )

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=True,
            registry=False,
            write=False,
            allow_drift=True,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["status"] == "actionable"  # nosec B101
    assert report["actions"] == [  # nosec B101
        {
            "can_write": False,
            "class": "release-checkout-missing",
            "command": "",
            "component": "",
            "detail": "checkout path missing; release state unavailable",
            "kind": "release",
            "provenance": "local-missing",
            "repo": "example-aio",
            "severity": "warning",
        }
    ]


def test_standards_reconcile_write_applies_safe_local_fixes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    (repo_path / ".trunk" / "actions").mkdir(parents=True)

    monkeypatch.setattr(cli, "release_plan_rows_for_repo", lambda *_args, **_kwargs: [])

    result = cmd_standards_reconcile(
        Namespace(
            manifest=str(manifest),
            repo=None,
            github=False,
            policy="unused.yml",
            release=False,
            registry=False,
            write=True,
            allow_drift=True,
            format="json",
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0  # nosec B101
    assert report["summary"]["applied"] == 2  # nosec B101
    assert (repo_path / ".aio-fleet.yml").exists()  # nosec B101
    assert not (repo_path / ".trunk").exists()  # nosec B101


def test_check_run_dry_run_outputs_payload(tmp_path: Path, capsys) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    result = cmd_check_run(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            sha="e" * 40,
            event="pull_request",
            status="completed",
            conclusion=None,
            summary="central validation passed",
            details_url=None,
            dry_run=True,
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "aio-fleet / required"  # nosec B101
    assert payload["conclusion"] == "success"  # nosec B101


def test_doctor_outputs_json_failure_classes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.setattr(
        cli,
        "fleet_doctor_report",
        lambda *_args, **_kwargs: {
            "checks": [
                {
                    "name": "publish-credentials",
                    "status": "failed",
                    "class": "credential-gap",
                    "detail": "missing DOCKERHUB_TOKEN",
                }
            ]
        },
    )

    result = cmd_doctor(
        Namespace(
            manifest=str(manifest),
            repo=None,
            no_local=False,
            app_checks=False,
            publish=True,
            cleanup=False,
            alerts=False,
            live_auth=False,
            check_delete_scope=False,
            require_alerts=False,
            no_manifest_checks=True,
            github=False,
            policy="unused.yml",
            check_secrets=False,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["failure_classes"] == ["credential-gap"]  # nosec B101


def test_workflow_control_report_writes_bootstrap_failure(
    tmp_path: Path, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    result = cmd_workflow_control_report(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            sha="a" * 40,
            event="push",
            source="main",
            publish=True,
            publish_component=["aio"],
            failure=["app-check-permission: bootstrap check-run failed: forbidden"],
            status="failure",
            output=str(output),
            transaction_id="example-transaction",
            check_mode=CHECK_MODE_FULL,
            changed_paths_json="",
            fast_path_reason="",
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(output.read_text())
    assert report["status"] == "failure"  # nosec B101
    assert report["transaction_id"] == "example-transaction"  # nosec B101
    assert report["publish_attestation"]["publish_components"] == ["aio"]  # nosec B101
    assert (  # nosec B101
        report["publish_attestation"]["transaction_id"] == "example-transaction"
    )
    assert "app-check-permission" in report["failure_classes"]  # nosec B101
    assert json.loads(capsys.readouterr().out)["repo"] == "example-aio"  # nosec B101


def test_workflow_control_report_writes_fast_path_metadata(
    tmp_path: Path,
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    result = cmd_workflow_control_report(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            sha="a" * 40,
            event="pull_request",
            source="pr:7",
            publish=False,
            publish_component=[],
            failure=[],
            status="success",
            output=str(output),
            transaction_id="",
            check_mode=CHECK_MODE_FAST_CLEANUP,
            changed_paths_json=json.dumps([".trunk/trunk.yaml"]),
            fast_path_reason="cleanup/local-hygiene-only paths",
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(output.read_text())
    assert report["check_mode"] == CHECK_MODE_FAST_CLEANUP  # nosec B101
    assert report["fast_path"] == {  # nosec B101
        "enabled": True,
        "reason": "cleanup/local-hygiene-only paths",
        "changed_paths": [".trunk/trunk.yaml"],
        "changed_files": [],
    }


def test_workflow_control_report_rejects_invalid_fast_path_scope(
    tmp_path: Path,
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    result = cmd_workflow_control_report(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            sha="a" * 40,
            event="pull_request",
            source="pr:7",
            publish=False,
            publish_component=[],
            failure=[],
            status="success",
            output=str(output),
            transaction_id="",
            check_mode=CHECK_MODE_FAST_CLEANUP,
            changed_paths_json=json.dumps(["Dockerfile"]),
            fast_path_reason="cleanup/local-hygiene-only paths",
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(output.read_text())
    assert report["status"] == "failure"  # nosec B101
    assert report["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert report["fast_path"] == {  # nosec B101
        "enabled": False,
        "reason": "publish/catalog path: Dockerfile",
        "changed_paths": ["Dockerfile"],
        "changed_files": [],
    }
    assert report["failures"][0].startswith("fast-path-scope:")  # nosec B101


def test_control_check_changed_paths_fast_path_writes_report_without_steps(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    monkeypatch.setattr(
        cli,
        "central_check_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )
    monkeypatch.setattr(
        cli,
        "run_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            changed_paths_json=json.dumps([".trunk/trunk.yaml"]),
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(output.read_text())
    assert report["status"] == "success"  # nosec B101
    assert report["check_mode"] == CHECK_MODE_FAST_CLEANUP  # nosec B101
    assert report["fast_path"]["changed_paths"] == [".trunk/trunk.yaml"]  # nosec B101


def test_control_check_no_fast_path_runs_full_steps(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"
    called = {}

    monkeypatch.setattr(cli, "central_check_steps", lambda *args, **kwargs: ["step"])

    def fake_run_steps(steps, *, dry_run: bool) -> list[str]:
        called["steps"] = steps
        called["dry_run"] = dry_run
        return []

    monkeypatch.setattr(cli, "run_steps", fake_run_steps)

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            changed_paths_json=json.dumps([".trunk/trunk.yaml"]),
            no_fast_path=True,
        )
    )

    assert result == 0  # nosec B101
    assert called == {"steps": ["step"], "dry_run": False}  # nosec B101
    report = json.loads(output.read_text())
    assert report["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert report["fast_path"] == {  # nosec B101
        "enabled": False,
        "reason": "fast path disabled",
        "changed_paths": [".trunk/trunk.yaml"],
        "changed_files": [],
    }


def test_control_check_fast_path_only_rejects_full_scope_without_steps(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    monkeypatch.setattr(
        cli,
        "central_check_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )
    monkeypatch.setattr(
        cli,
        "run_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            changed_paths_json=json.dumps(["Dockerfile"]),
            fast_path_only=True,
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(output.read_text())
    assert report["status"] == "failure"  # nosec B101
    assert report["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert report["failures"][0].startswith("fast-path-scope:")  # nosec B101


def test_control_check_resolves_changed_files_before_fast_path(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    monkeypatch.setattr(
        cli,
        "resolve_changed_files",
        lambda *args, **kwargs: [{"path": "scripts/release.py", "status": "removed"}],
    )
    monkeypatch.setattr(
        cli,
        "central_check_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            resolve_changed_files=True,
            fast_path_only=True,
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(output.read_text())
    assert report["check_mode"] == CHECK_MODE_FAST_CLEANUP  # nosec B101
    assert report["fast_path"]["changed_files"] == [  # nosec B101
        {"path": "scripts/release.py", "status": "removed"}
    ]


def test_control_check_rejects_resolved_rename_from_required_path(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    monkeypatch.setattr(
        cli,
        "resolve_changed_files",
        lambda *args, **kwargs: [
            {"path": "docs/Dockerfile", "status": "renamed"},
            {"path": "Dockerfile", "status": "renamed-from"},
        ],
    )
    monkeypatch.setattr(
        cli,
        "central_check_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            resolve_changed_files=True,
            fast_path_only=True,
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(output.read_text())
    assert report["status"] == "failure"  # nosec B101
    assert report["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert "Dockerfile" in report["fast_path"]["changed_paths"]  # nosec B101
    assert report["fast_path"]["changed_files"] == [  # nosec B101
        {"path": "docs/Dockerfile", "status": "renamed"},
        {"path": "Dockerfile", "status": "renamed-from"},
    ]


def test_control_check_changed_files_json_expands_previous_path(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    output = tmp_path / "control-report.json"

    monkeypatch.setattr(
        cli,
        "central_check_steps",
        lambda *args, **kwargs: pytest.fail("full central steps should be skipped"),
    )

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            report_json=str(output),
            changed_files_json=json.dumps(
                [
                    {
                        "path": "docs/Dockerfile",
                        "status": "renamed",
                        "previous_path": "Dockerfile",
                    }
                ]
            ),
            fast_path_only=True,
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(output.read_text())
    assert report["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert report["fast_path"]["changed_paths"] == [  # nosec B101
        "docs/Dockerfile",
        "Dockerfile",
    ]


def test_control_check_rejects_invalid_changed_paths_json(
    tmp_path: Path, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    result = cmd_control_check(
        _control_check_namespace(
            manifest,
            changed_paths_json='{"not":"a-list"}',
        )
    )

    assert result == 1  # nosec B101
    assert "--changed-paths-json" in capsys.readouterr().err  # nosec B101


def test_alert_doctor_warns_without_required_alerts(capsys) -> None:
    result = cmd_alert_doctor(
        Namespace(
            kuma_url="",
            webhook_url="",
            require_alerts=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["warnings"]  # nosec B101
    assert report["ok"] is True  # nosec B101


def test_alert_doctor_can_require_alerts(capsys) -> None:
    result = cmd_alert_doctor(
        Namespace(
            kuma_url="",
            webhook_url="",
            require_alerts=True,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["findings"]  # nosec B101


def test_alert_test_forces_webhook(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "emit_alert",
        lambda *_args, **_kwargs: {"kuma": "would-send", "webhook": "would-send"},
    )

    result = cmd_alert_test(
        Namespace(
            event="upstream-update",
            status="warning",
            summary="test",
            repo=None,
            component=None,
            dedupe_key=None,
            details_url=None,
            kuma_url="https://kuma",
            webhook_url="https://hook",
            webhook_format="json",
            dry_run=True,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["webhook"] == "would-send"  # nosec B101


def test_alert_send_adds_sanitized_failure_file_annotations(
    tmp_path: Path, capsys
) -> None:
    failure_file = tmp_path / "fleet-dashboard.err"
    failure_file.write_text(
        "\x1b[31mTraceback line\x1b[0m\n"
        "/Users/shadowbook/Documents/aio-fleet/.venv/bin/python failed\n"
        "OSError: [Errno 7] Argument list too long: 'gh'\n"
        "extra line 1\n"
        "extra line 2\n"
        "extra line 3\n"
    )
    missing_file = tmp_path / "missing.err"

    result = cmd_alert_send(
        Namespace(
            event="control-plane",
            status="failure",
            summary="dashboard failed",
            repo=None,
            component=None,
            dedupe_key=None,
            details_url="https://github.com/wgross19/aio-fleet/actions/runs/1",
            annotation=None,
            failure_file=[str(missing_file), str(failure_file)],
            report_json=None,
            kuma_url=None,
            webhook_url=None,
            webhook_format="json",
            force_webhook=False,
            dry_run=True,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    annotations = report["payload"]["annotations"]
    assert len(annotations) == 1  # nosec B101
    assert annotations[0].startswith(  # nosec B101
        "failure excerpt fleet-dashboard.err: Traceback line"
    )
    assert "Argument list too long" in annotations[0]  # nosec B101
    assert "/Users/shadowbook" not in annotations[0]  # nosec B101
    assert "\x1b" not in annotations[0]  # nosec B101
    assert "extra line 3" not in annotations[0]  # nosec B101


def test_poll_missing_checks_only_skips_satisfied_targets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    repo = load_manifest(manifest).repo("example-aio")

    monkeypatch.setattr(
        cli,
        "poll_targets",
        lambda *args, **kwargs: [
            PollTarget(
                repo=repo,
                sha="f" * 40,
                event="pull_request",
                source="pr:1",
            )
        ],
    )
    monkeypatch.setattr(cli, "check_run_satisfied", lambda *args, **kwargs: True)

    result = cmd_poll(
        Namespace(
            manifest=str(manifest),
            no_prs=False,
            no_main=False,
            create_checks=False,
            missing_checks_only=True,
            dry_run=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    assert json.loads(capsys.readouterr().out) == {"targets": []}  # nosec B101


def test_fleet_dashboard_update_dry_run_outputs_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setattr(
        cli,
        "dashboard_report",
        lambda *_args, **_kwargs: {
            "body": "# Dashboard\n",
            "state": {"rows": [{"repo": "example-aio"}]},
        },
    )
    monkeypatch.setattr(
        cli,
        "upsert_dashboard_issue",
        lambda **_kwargs: SimpleNamespace(
            action="would-create",
            number=None,
            url="",
        ),
    )

    result = cmd_fleet_dashboard_update(
        Namespace(
            manifest=str(manifest),
            issue_repo="wgross19/aio-fleet",
            issue_number=None,
            registry=False,
            write=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "would-create"  # nosec B101
    assert report["state"]["rows"][0]["repo"] == "example-aio"  # nosec B101


def test_fleet_report_generate_outputs_stable_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setattr(
        cli,
        "dashboard_report",
        lambda *_args, **_kwargs: {
            "body": "# Dashboard\n",
            "state": {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "issue_repo": "wgross19/aio-fleet",
                "warnings": [],
                "summary": {"posture": "green"},
                "rows": [{"repo": "example-aio"}],
                "actions": [],
                "failures": [],
                "approvals": [],
                "catalog": {"state": "ready"},
                "standards": {"state": "ok"},
                "candidates": {"state": "planning"},
                "activity": [],
                "destination_repos": [],
                "rehab_repos": [],
                "registry": [],
                "releases": [],
                "cleanup": [],
                "workflow": {},
            },
        },
    )

    result = cmd_fleet_report_generate(
        Namespace(
            manifest=str(manifest),
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=True,
            stale_days=7,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 4  # nosec B101
    assert report["summary"]["posture"] == "green"  # nosec B101
    assert report["rows"][0]["repo"] == "example-aio"  # nosec B101


def test_fleet_report_generate_redacts_public_text(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    unsafe_path = "/Users/shadowbook/Documents/aio-fleet/.venv/bin/python"
    unsafe_webhook = "https://discord.com/api/webhooks/123/secret"

    monkeypatch.setattr(
        cli,
        "dashboard_report",
        lambda *_args, **_kwargs: {
            "body": "# Dashboard\n",
            "state": {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "issue_repo": "wgross19/aio-fleet",
                "warnings": [unsafe_webhook],
                "summary": {"posture": "blocked"},
                "rows": [{"repo": "example-aio", "next_action": unsafe_path}],
                "actions": [],
                "failures": [],
                "approvals": [],
                "catalog": {"state": "ready"},
                "standards": {"state": "ok"},
                "candidates": {"state": "planning"},
                "activity": [],
                "destination_repos": [],
                "rehab_repos": [],
                "registry": [],
                "releases": [],
                "cleanup": [],
                "workflow": {},
            },
        },
    )

    result = cmd_fleet_report_generate(
        Namespace(
            manifest=str(manifest),
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=True,
            stale_days=7,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert unsafe_path not in output  # nosec B101
    assert unsafe_webhook not in output  # nosec B101
    report = json.loads(output)
    assert report["warnings"] == ["<redacted: Discord webhook URL>"]  # nosec B101
    assert (  # nosec B101
        report["rows"][0]["next_action"] == "<redacted: macOS home path>"
    )


def test_fleet_report_closeout_splits_remote_and_local_posture(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setattr(
        cli,
        "dashboard_report",
        lambda *_args, **_kwargs: {
            "body": "# Dashboard\n",
            "state": {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "issue_repo": "wgross19/aio-fleet",
                "warnings": [],
                "summary": {
                    "posture": "green",
                    "remote_posture": "green",
                    "local_posture": "hygiene",
                    "open_prs": 0,
                    "upstream_updates": 0,
                    "release_due": 0,
                    "publish_missing": 0,
                    "catalog_state": "ready",
                    "standards_state": "ok",
                    "workflow_state": "success",
                    "pending_approvals": 0,
                    "alert_warnings": 0,
                    "local_hygiene": 1,
                },
                "actions": [
                    {
                        "id": "remote",
                        "repo": "example-aio",
                        "kind": "registry-publish",
                        "provenance": "remote-confirmed",
                    }
                ],
                "cleanup": [
                    {
                        "repo": "signoz-aio",
                        "state": "local-only",
                        "provenance": "local-only",
                        "findings_count": 0,
                        "findings": [],
                        "local_findings_count": 1,
                        "local_findings": [
                            {
                                "path": ".trunk",
                                "reason": "local scratch overlay",
                                "provenance": "local-only",
                            }
                        ],
                    }
                ],
            },
        },
    )

    result = cmd_fleet_report_closeout(
        Namespace(
            manifest=str(manifest),
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=True,
            stale_days=7,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_posture"] == "green"  # nosec B101
    assert payload["local_posture"] == "hygiene"  # nosec B101
    assert payload["summary"]["remote_actions"] == 1  # nosec B101
    assert payload["local_hygiene"][0]["cleanup_command"] == (  # nosec B101
        "uv run aio-fleet cleanup-repo --repo signoz-aio --fix --verify"
    )


@pytest.mark.parametrize(
    "command",
    [
        ["fleet-report", "closeout"],
        ["fleet-queue", "generate"],
        ["fleet-queue", "dispatch", "--id", "registry-publish:example-aio:aio"],
        ["fleetbot", "render-command", "--command", "closeout"],
        ["registry", "delete-dockerhub-tags"],
        ["workflow", "checkout-upstream"],
        ["workflow", "upstream-monitor"],
        ["workflow", "upstream-validate"],
        ["workflow", "upstream-actions"],
    ],
)
def test_nested_manifest_options_preserve_global_manifest(
    command: list[str],
) -> None:
    args = cli.build_parser().parse_args(
        ["--manifest", "fleet-dashboard.manifest.yml", *command]
    )

    assert args.manifest == "fleet-dashboard.manifest.yml"  # nosec B101


def test_nested_manifest_options_can_override_global_manifest() -> None:
    args = cli.build_parser().parse_args(
        [
            "--manifest",
            "fleet-dashboard.manifest.yml",
            "fleet-report",
            "closeout",
            "--manifest",
            "fleet-closeout.manifest.yml",
        ]
    )

    assert args.manifest == "fleet-closeout.manifest.yml"  # nosec B101


def test_fleet_report_schema_and_validate(tmp_path: Path, capsys) -> None:
    result = cmd_fleet_report_schema(Namespace())

    assert result == 0  # nosec B101
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["schema_version"]["const"] == 4  # nosec B101
    assert "rows" in schema["required"]  # nosec B101

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "issue_repo": "wgross19/aio-fleet",
                "warnings": [],
                "summary": {},
                "rows": [],
                "actions": [],
                "failures": [],
                "approvals": [],
                "catalog": {},
                "standards": {},
                "candidates": {},
                "activity": [],
                "destination_repos": [],
                "rehab_repos": [],
                "registry": [],
                "releases": [],
                "cleanup": [],
                "workflow": {},
            }
        )
    )

    result = cmd_fleet_report_validate(Namespace(input=str(report), format="json"))

    assert result == 0  # nosec B101
    assert json.loads(capsys.readouterr().out)["ok"] is True  # nosec B101


def test_fleet_report_validate_rejects_schema_drift(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"schema_version": 2, "rows": []}))

    result = cmd_fleet_report_validate(Namespace(input=str(report), format="json"))

    assert result == 1  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False  # nosec B101
    assert any(
        "unsupported schema_version" in failure for failure in payload["failures"]
    )  # nosec B101


def test_fleet_report_explain_run_classifies_log_file(tmp_path: Path, capsys) -> None:
    log = tmp_path / "failed.log"
    log.write_text("permission_denied: write_package for ghcr.io/wgross19/sure-aio")

    result = cmd_fleet_report_explain_run(
        Namespace(
            run_id="12345",
            issue_repo="wgross19/aio-fleet",
            log_file=str(log),
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["classification"]["root_cause"] == "ghcr-access"  # nosec B101


def test_fleet_report_explain_run_omits_raw_failed_gh_output(
    monkeypatch, capsys
) -> None:
    unsafe_path = "/Users/shadowbook/Documents/aio-fleet/.venv/bin/python"

    def fake_run(*_args: object, **_kwargs: object):
        return subprocess.CompletedProcess(
            args=["gh"],
            returncode=1,
            stdout="",
            stderr=f"pytest failed from {unsafe_path}",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = cmd_fleet_report_explain_run(
        Namespace(
            run_id="12345",
            issue_repo="wgross19/aio-fleet",
            log_file="",
            format="json",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert unsafe_path not in output  # nosec B101
    assert payload["command_error"] == (  # nosec B101
        "gh run view failed; raw output omitted from report"
    )


def test_fleet_queue_generate_from_report_input(tmp_path: Path, capsys) -> None:
    sha = "a" * 40
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "summary": {},
                "releases": [
                    {
                        "repo": "sure-aio",
                        "component": "aio",
                        "state": "publish-missing",
                        "sha": sha,
                        "registry_verified": True,
                        "registry_failures": ["wgross19/sure-aio:latest: missing"],
                        "operator_commands": {
                            "release_transaction": (
                                "uv run aio-fleet release transaction "
                                f"--repo sure-aio --component aio --sha {sha} --dry-run"
                            )
                        },
                    }
                ],
            }
        )
    )

    result = cmd_fleet_queue_generate(
        Namespace(
            input=str(report),
            manifest="fleet.yml",
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=False,
            stale_days=7,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["actions"][0]["kind"] == "registry-publish"  # nosec B101


def test_fleet_queue_dispatch_dry_run_from_report_input(tmp_path: Path, capsys) -> None:
    sha = "a" * 40
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "summary": {},
                "releases": [
                    {
                        "repo": "sure-aio",
                        "component": "aio",
                        "state": "publish-missing",
                        "sha": sha,
                        "registry_verified": True,
                        "registry_failures": ["wgross19/sure-aio:latest: missing"],
                    }
                ],
            }
        )
    )
    queue_payload = _fleet_queue_generate_for_test(report, capsys)
    action_id = json.loads(queue_payload)["actions"][0]["id"]

    result = cmd_fleet_queue_dispatch(
        Namespace(
            id=action_id,
            input=str(report),
            manifest="fleet.yml",
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=False,
            stale_days=7,
            dry_run=True,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_dispatch"] is True  # nosec B101
    assert "gh workflow run control-plane.yml" in payload["command"]  # nosec B101


def test_fleetbot_render_command_from_report_input(tmp_path: Path, capsys) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "generated_at": "2026-05-05T00:00:00+00:00",
                "summary": {"posture": "green"},
                "rows": [],
                "releases": [],
                "actions": [],
                "failures": [],
            }
        )
    )

    result = cmd_fleetbot_render_command(
        Namespace(
            command_name="status",
            repo="",
            run_id="",
            input=str(report),
            manifest="fleet.yml",
            issue_repo="wgross19/aio-fleet",
            registry=False,
            include_activity=False,
            stale_days=7,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "status"  # nosec B101
    assert payload["visibility"] == "read-only"  # nosec B101


def _fleet_queue_generate_for_test(report: Path, capsys) -> str:
    cmd_fleet_queue_generate(
        Namespace(
            input=str(report),
            manifest="fleet.yml",
            issue_repo="wgross19/aio-fleet",
            registry=True,
            include_activity=False,
            stale_days=7,
            format="json",
        )
    )
    return capsys.readouterr().out


def test_release_plan_outputs_all_repo_states(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    captured: dict[str, object] = {}

    def fake_release_plan_for_manifest(*_args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "repo": "example-aio",
                "state": "release-due",
                "next_version": "1.0.0-aio.2",
                "next_action": "uv run aio-fleet release prepare --repo example-aio --dry-run",
            }
        ]

    monkeypatch.setattr(
        cli, "release_plan_for_manifest", fake_release_plan_for_manifest
    )

    result = cmd_release_plan(
        Namespace(
            manifest=str(manifest),
            all=True,
            repo=None,
            repo_path=None,
            registry=False,
            registry_verify_attempts=1,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["release_due"] == 1  # nosec B101
    assert payload["repos"][0]["state"] == "release-due"  # nosec B101
    assert captured["redact_private"] is True  # nosec B101
    assert captured["registry_verify_attempts"] == 1  # nosec B101


def test_release_reconcile_routes_publish_through_transaction(
    tmp_path: Path, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    release_plan = tmp_path / "release-plan.json"
    release_plan.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "example-aio",
                        "component": "aio",
                        "state": "publish-missing",
                        "next_action": (
                            "uv run aio-fleet release transaction "
                            "--repo example-aio --component aio "
                            "--sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --dry-run"
                        ),
                    }
                ]
            }
        )
    )

    result = cmd_release_reconcile(
        Namespace(
            manifest=str(manifest),
            input=str(release_plan),
            repo=None,
            component=None,
            repo_path=None,
            all=False,
            registry=False,
            create_upstream_prs=False,
            write=False,
            dry_run=True,
            post_check=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["publish"] == 1  # nosec B101
    assert payload["actions"][0]["action"] == "publish"  # nosec B101
    assert "release transaction" in payload["actions"][0]["command"]  # nosec B101
    assert "--component aio" in payload["actions"][0]["command"]  # nosec B101


def test_security_audit_workflows_reports_findings(tmp_path: Path, capsys) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("""
name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: echo hello
""")

    result = cmd_security_audit_workflows(Namespace(path=str(tmp_path), format="json"))

    assert result == 1  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    codes = {finding["code"] for finding in payload["findings"]}
    assert "unpinned-action" in codes  # nosec B101
    assert "checkout-credentials" in codes  # nosec B101


def test_promote_rehab_blocks_legacy_repo(tmp_path: Path, capsys) -> None:
    rehab_path = tmp_path / "legacy-aio"
    rehab_path.mkdir()
    (rehab_path / "cliff.toml").write_text("[changelog]\n")
    active_path = tmp_path / "example-aio"
    active_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  rehab_repos:
    legacy-aio:
      path: {rehab_path}
      github_repo: wgross19/legacy-aio
repos:
  example-aio:
    path: {active_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")

    result = cmd_promote_rehab(
        Namespace(
            manifest=str(manifest),
            repo="legacy-aio",
            profile="changelog-version",
            dry_run=True,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False  # nosec B101
    assert "retired shared path" in payload["findings"][0]  # nosec B101


def test_fleet_dashboard_commands_outputs_github_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "dashboard_issue_commands",
        lambda **_kwargs: {
            "is_dashboard": True,
            "requested": True,
            "commands": {"rescan": True, "upstream_monitor": False},
        },
    )

    result = cmd_fleet_dashboard_commands(
        Namespace(
            issue_repo="wgross19/aio-fleet",
            issue_number=55,
            format="github-output",
        )
    )

    assert result == 0  # nosec B101
    assert capsys.readouterr().out.splitlines() == [  # nosec B101
        "is_dashboard=true",
        "requested=true",
        "rescan=true",
        "upstream_monitor=false",
        "standards_reconcile=false",
        "queue_publish_checks=false",
    ]


def test_poll_does_not_publish_template_profile_targets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  unraid-aio-template:
    path: {repo_path}
    public: true
    app_slug: unraid-aio-template
    image_name: wgross19/unraid-aio-template
    docker_cache_scope: unraid-aio-template-image
    pytest_image_tag: unraid-aio-template:pytest
    publish_profile: template
""")
    repo = load_manifest(manifest).repo("unraid-aio-template")
    monkeypatch.setattr(
        cli,
        "poll_targets",
        lambda *args, **kwargs: [
            PollTarget(
                repo=repo,
                sha="f" * 40,
                event="push",
                source="main",
            )
        ],
    )

    result = cmd_poll(
        Namespace(
            manifest=str(manifest),
            no_prs=False,
            no_main=False,
            create_checks=False,
            missing_checks_only=False,
            dry_run=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"][0]["publish"] is False  # nosec B101


def test_export_app_manifest_prints_future_app_manifest(tmp_path: Path, capsys) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    result = cmd_export_app_manifest(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            output=None,
            write=False,
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "schema_version: 1" in output  # nosec B101
    assert "repo: example-aio" in output  # nosec B101


def _write_minimal_manifest(tmp_path: Path) -> tuple[Path, Path]:
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
""")
    return manifest, repo_path


def _control_check_namespace(
    manifest: Path,
    *,
    report_json: str = "",
    changed_paths_json: str = "",
    changed_files_json: str = "",
    resolve_changed_files: bool = False,
    no_fast_path: bool = False,
    fast_path_only: bool = False,
) -> Namespace:
    return Namespace(
        manifest=str(manifest),
        repo="example-aio",
        repo_path=None,
        sha="a" * 40,
        source="pr:7",
        event="pull_request",
        publish=False,
        publish_component=[],
        no_trunk=False,
        no_integration=False,
        no_github_prereleases=False,
        validation_only=False,
        publish_only=False,
        check_run=False,
        dry_run=False,
        report_json=report_json,
        transaction_id="",
        changed_paths_json=changed_paths_json,
        changed_files_json=changed_files_json,
        resolve_changed_files=resolve_changed_files,
        no_fast_path=no_fast_path,
        fast_path_only=fast_path_only,
    )


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(  # nosec B603 B607
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_release_repo(repo_path: Path, version: str) -> str:
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", f"chore(release): {version}")
    return _git(repo_path, "rev-parse", "HEAD")


def _write_alpha_prerelease_repo(tmp_path: Path) -> tuple[Path, Path, str]:
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.1 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.1-alpha.7-aio.1")
    return manifest, repo_path, _git(repo_path, "rev-parse", "HEAD")


def _write_control_report(
    path: Path,
    *,
    sha: str,
    status: str = "success",
    publish: bool = True,
    components: list[str] | None = None,
) -> None:
    component_names = components or ["sure-alpha"]
    path.write_text(
        json.dumps(
            {
                "repo": "sure-aio",
                "sha": sha,
                "event": "push",
                "source": "main",
                "publish": publish,
                "status": status,
                "failures": [],
                "publish_attestation": {
                    "repo": "sure-aio",
                    "expected_sha": sha,
                    "event": "push",
                    "source": "main",
                    "control_check_result": status,
                    "publish_requested": publish,
                    "publish_eligible": publish and status == "success",
                    "publish_components": component_names,
                },
                "components": [
                    {"component": component_name} for component_name in component_names
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


def test_release_prepare_dry_run_prepends_changelog_section(
    tmp_path: Path, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.0\n")
    (repo_path / "upstream.toml").write_text("[upstream]\n")
    (repo_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 0.9.0-aio.1 - 2026-01-01\n\n- old\n"
    )
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)

    result = cli.cmd_release_prepare(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            component="aio",
            dry_run=True,
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "--unreleased --prepend" in output  # nosec B101
    assert "--output" not in output  # nosec B101


def test_release_prepare_updates_registry_only_revision_arg(
    tmp_path: Path, monkeypatch
) -> None:
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        xml_paths:
          - sure-aio-alpha.xml
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.11\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text("# Alpha Changelog\n")
    (repo_path / "sure-aio-alpha.xml").write_text(
        "<Container><Changes>old</Changes></Container>\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 0.7.1-alpha.11-aio.1")
    _git(repo_path, "tag", "sure-alpha/0.7.1-alpha.11-aio.1")

    def fake_run(command: list[str], **kwargs):
        assert kwargs.get("cwd") == repo_path  # nosec B101
        assert command[:2] == ["git", "cliff"]  # nosec B101
        (repo_path / "CHANGELOG.alpha.md").write_text(
            "# Alpha Changelog\n\n"
            "## 0.7.1-alpha.11-aio.2 - 2026-06-01\n\n"
            "- Publish alpha proxy fixes.\n"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cli.cmd_release_prepare(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            repo_path=None,
            component="sure-alpha",
            dry_run=False,
        )
    )

    assert result == 0  # nosec B101
    assert (  # nosec B101
        "ARG AIO_REVISION=2" in (repo_path / "Dockerfile.alpha").read_text()
    )
    assert (
        "0.7.1-alpha.11-aio.2"
        in (repo_path / "CHANGELOG.alpha.md").read_text()  # nosec B101
    )
    assert (
        "Publish alpha proxy fixes"
        in (repo_path / "sure-aio-alpha.xml").read_text()  # nosec B101
    )


def test_release_prepare_reuses_existing_component_changelog_section(
    tmp_path: Path, monkeypatch
) -> None:
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        xml_paths:
          - sure-aio-alpha.xml
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.2-alpha.1\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "Alpha releases are testing builds for `sure-aio-alpha`.\n\n"
        "## 0.7.2-alpha.1-aio.1 - 2026-06-02\n\n"
        "### Build\n\n"
        "- Track upstream Sure Alpha 0.7.2-alpha.1.\n"
    )
    (repo_path / "sure-aio-alpha.xml").write_text(
        "<Container><Changes>old</Changes></Container>\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(sync): bump sure alpha")
    _git(repo_path, "tag", "sure-alpha/0.7.1-alpha.11-aio.2")

    def fail_run(command: list[str], **_kwargs):
        raise AssertionError(f"git-cliff should not run: {command}")

    monkeypatch.setattr(cli, "_run", fail_run)

    result = cli.cmd_release_prepare(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            repo_path=None,
            component="sure-alpha",
            dry_run=False,
        )
    )

    changelog = (repo_path / "CHANGELOG.alpha.md").read_text()
    template = (repo_path / "sure-aio-alpha.xml").read_text()
    assert result == 0  # nosec B101
    assert changelog.startswith("# Alpha Changelog\n")  # nosec B101
    assert "# Changelog" not in changelog  # nosec B101
    assert "Track upstream Sure Alpha 0.7.2-alpha.1" in template  # nosec B101


def test_release_publish_uses_changelog_version_for_changelog_profile(
    tmp_path: Path, capsys
) -> None:
    repo_path = tmp_path / "penpot-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  penpot-aio:
    path: {repo_path}
    public: true
    app_slug: penpot-aio
    image_name: wgross19/penpot-aio
    docker_cache_scope: penpot-aio-image
    pytest_image_tag: penpot-aio:pytest
    github_repo: wgross19/penpot-aio
    publish_profile: changelog-version
""")
    (repo_path / "Dockerfile").write_text("ARG PENPOT_VERSION=2.15.3\n")
    (repo_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## v2.15.3-aio.1 - 2026-05-20\n\n"
        "- Package Penpot 2.15.3 as an AIO image.\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.15.3-aio.1")

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="penpot-aio",
            component="aio",
            repo_path=None,
            dry_run=True,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create v2.15.3-aio.1" in output  # nosec B101
    assert "--title v2.15.3-aio.1" in output  # nosec B101
    assert "--notes '- Package Penpot 2.15.3 as an AIO image.'" in output  # nosec B101


def test_release_publish_preserves_existing_v_prefix_for_changelog_profile(
    tmp_path: Path, capsys
) -> None:
    repo_path = tmp_path / "penpot-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  penpot-aio:
    path: {repo_path}
    public: true
    app_slug: penpot-aio
    image_name: wgross19/penpot-aio
    docker_cache_scope: penpot-aio-image
    pytest_image_tag: penpot-aio:pytest
    github_repo: wgross19/penpot-aio
    publish_profile: changelog-version
""")
    (repo_path / "Dockerfile").write_text("ARG PENPOT_VERSION=2.15.4\n")
    (repo_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## v2.15.3-aio.4 - 2026-05-31\n\n"
        "- Package Penpot 2.15.3 as an AIO image.\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.15.3-aio.4")
    _git(repo_path, "tag", "v2.15.3-aio.4")
    (repo_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## 2.15.4-aio.1 - 2026-06-02\n\n"
        "- Update Penpot to 2.15.4.\n\n"
        "## v2.15.3-aio.4 - 2026-05-31\n\n"
        "- Package Penpot 2.15.3 as an AIO image.\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): 2.15.4-aio.1")

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="penpot-aio",
            component="aio",
            repo_path=None,
            dry_run=True,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create v2.15.4-aio.1" in output  # nosec B101
    assert "--title v2.15.4-aio.1" in output  # nosec B101
    assert "--notes '- Update Penpot to 2.15.4.'" in output  # nosec B101


def _changelog_version_release_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_path = tmp_path / "penpot-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  penpot-aio:
    path: {repo_path}
    public: true
    app_slug: penpot-aio
    image_name: wgross19/penpot-aio
    docker_cache_scope: penpot-aio-image
    pytest_image_tag: penpot-aio:pytest
    github_repo: wgross19/penpot-aio
    publish_profile: changelog-version
""")
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=2.15.4\n")
    (repo_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "## v2.15.4-aio.1 - 2026-06-02\n\n"
        "- Update Penpot to 2.15.4.\n"
    )
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "tests@example.invalid")
    _git(repo_path, "config", "user.name", "aio-fleet tests")
    _git(repo_path, "config", "commit.gpgsign", "false")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-m", "chore(release): v2.15.4-aio.1")
    return manifest, repo_path


def test_release_publish_passes_release_token_to_gh(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _changelog_version_release_repo(tmp_path)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token-xyz")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(
        command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> SimpleNamespace:
        calls.append((command, env))
        if "extract-release-notes" in command:
            return SimpleNamespace(
                returncode=0, stdout="- Update Penpot to 2.15.4.\n", stderr=""
            )
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="release not found")
        if command[:3] == ["gh", "release", "create"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="penpot-aio",
            component="aio",
            repo_path=None,
            dry_run=False,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    create_calls = [
        (cmd, env) for (cmd, env) in calls if cmd[:3] == ["gh", "release", "create"]
    ]
    assert create_calls  # nosec B101
    _cmd, create_env = create_calls[0]
    assert create_env is not None  # nosec B101
    # gh must receive the release token as GH_TOKEN, not just AIO_FLEET_RELEASE_TOKEN.
    assert create_env.get("GH_TOKEN") == "release-token-xyz"  # nosec B101


def test_release_publish_edits_existing_release_idempotently(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _changelog_version_release_repo(tmp_path)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token-xyz")

    actions: list[str] = []

    def fake_run(
        command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> SimpleNamespace:
        if "extract-release-notes" in command:
            return SimpleNamespace(
                returncode=0, stdout="- Update Penpot to 2.15.4.\n", stderr=""
            )
        if command[:3] == ["gh", "release", "view"]:
            # Release already exists.
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if command[:2] == ["gh", "release"] and command[2] in {"create", "edit"}:
            actions.append(command[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="penpot-aio",
            component="aio",
            repo_path=None,
            dry_run=False,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    # An existing release is edited in place, never re-created.
    assert actions == ["edit"]  # nosec B101


def test_release_publish_skips_registry_only_formal_release(
    tmp_path: Path, capsys
) -> None:
    repo_path = tmp_path / "nanoclaw-aio"
    repo_path.mkdir()
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
    github_repo: wgross19/nanoclaw-aio
    publish_profile: multi-component
    components:
      agent:
        image_name: wgross19/nanoclaw-agent
        dockerfile: components/nanoclaw-agent/Dockerfile
        release_policy: registry_only
        release_suffix: agent
""")
    report_json = tmp_path / "release-report.json"

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="nanoclaw-aio",
            component="agent",
            repo_path=None,
            dry_run=False,
            report_json=str(report_json),
            format="json",
        )
    )

    assert result == 0  # nosec B101
    output = json.loads(capsys.readouterr().out)
    report = json.loads(report_json.read_text())
    assert output == report  # nosec B101
    assert report == {  # nosec B101
        "component": "agent",
        "reason": "registry_only",
        "repo": "nanoclaw-aio",
        "status": "skipped",
    }


def test_release_prepare_emits_authoritative_version_to_github_output(
    tmp_path: Path,
) -> None:
    manifest, _repo_path = _changelog_version_release_repo(tmp_path)
    output = tmp_path / "gh_output.txt"

    result = cli.cmd_release_prepare(
        Namespace(
            manifest=str(manifest),
            repo="penpot-aio",
            component="aio",
            repo_path=None,
            dry_run=True,
            github_output=str(output),
        )
    )

    assert result == 0  # nosec B101
    # The CLI emits the version so the workflow never has to grep CHANGELOG.md.
    assert "version=2.15.4-aio.1" in output.read_text()  # nosec B101


def test_release_publish_dry_run_creates_alpha_prerelease_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.1 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )
    _init_release_repo(repo_path, "0.7.1-alpha.7-aio.1")
    report_json = tmp_path / "release-report.json"

    def fake_run(command: list[str], **kwargs):
        del kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component="sure-alpha",
            repo_path=None,
            dry_run=True,
            report_json=str(report_json),
            format="json",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create sure-alpha/0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert "--title 0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert "--prerelease --latest=false" in output  # nosec B101
    report = json.loads(report_json.read_text())
    assert report["action"] == "would-create"  # nosec B101
    assert report["tag"] == "sure-alpha/0.7.1-alpha.7-aio.1"  # nosec B101
    assert report["release_package_tag"] == "0.7.1-alpha.7-aio.1"  # nosec B101
    assert "sure-alpha%2F0.7.1-alpha.7-aio.1" in report["url"]  # nosec B101


def test_release_publish_dry_run_allows_vprefixed_alpha_changelog_version(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## v0.7.1-alpha.7-aio.1 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )
    _init_release_repo(repo_path, "v0.7.1-alpha.7-aio.1")

    def fake_run(command: list[str], **kwargs):
        del kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component="sure-alpha",
            repo_path=None,
            dry_run=True,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create sure-alpha/v0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert "--title v0.7.1-alpha.7-aio.1" in output  # nosec B101


def test_release_publish_dry_run_updates_existing_alpha_prerelease_notes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.1 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )
    target_sha = _init_release_repo(repo_path, "0.7.1-alpha.7-aio.1")

    def fake_run(command: list[str], **kwargs):
        del kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{target_sha}\n", stderr="")
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=0, stdout=f"{target_sha}\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component="sure-alpha",
            repo_path=None,
            dry_run=True,
            report_json=None,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release edit sure-alpha/0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert "--target" not in output  # nosec B101
    assert (
        "prerelease=would-update sure-alpha/0.7.1-alpha.7-aio.1" in output
    )  # nosec B101


def test_release_publish_refuses_alpha_prerelease_target_drift(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=1\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.1 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )
    target_sha = _init_release_repo(repo_path, "0.7.1-alpha.7-aio.1")
    existing_sha = "a" * 40

    def fake_run(command: list[str], **kwargs):
        del kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{target_sha}\n", stderr="")
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=0, stdout=f"{existing_sha}\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    try:
        cmd_release_publish(
            Namespace(
                manifest=str(manifest),
                repo="sure-aio",
                component="sure-alpha",
                repo_path=None,
                dry_run=True,
                report_json=None,
                format="text",
            )
        )
    except SystemExit as exc:
        assert exc.code == 1  # nosec B101
    else:
        raise AssertionError("expected SystemExit for immutable target drift")

    captured = capsys.readouterr()
    assert "refusing to retarget immutable release" in captured.err  # nosec B101
    assert "Bump the component AIO revision" in captured.err  # nosec B101


def test_release_publish_refuses_alpha_registry_release_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo_path = tmp_path / "repo"
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        upstream_config: upstream.toml
        upstream_version_key: UPSTREAM_VERSION
        release_policy: registry_only
        release_history: github_prerelease
        release_changelog: CHANGELOG.alpha.md
        release_tag_prefix: sure-alpha/
        release_suffix: aio
        registry_revision_arg: AIO_REVISION
        github_release_latest: false
""")
    (repo_path / "Dockerfile.alpha").write_text(
        "ARG UPSTREAM_VERSION=0.7.1-alpha.7\nARG AIO_REVISION=2\n"
    )
    (repo_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_VERSION"\n'
    )
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.3 - 2026-05-18\n\n"
        "- alpha release notes\n"
    )

    def fake_run(command: list[str], **kwargs):
        del kwargs
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    try:
        cmd_release_publish(
            Namespace(
                manifest=str(manifest),
                repo="sure-aio",
                component="sure-alpha",
                repo_path=None,
                dry_run=True,
                report_json=None,
                format="text",
            )
        )
    except SystemExit as exc:
        assert exc.code == 1  # nosec B101
    else:
        raise AssertionError("expected SystemExit for registry release mismatch")

    captured = capsys.readouterr()
    assert "release changelog version 0.7.1-alpha.7-aio.3" in captured.err  # nosec B101
    assert "registry package tag 0.7.1-alpha.7-aio.2" in captured.err  # nosec B101


def test_prerelease_publish_refuses_mutated_checkout_after_control_report(
    tmp_path: Path, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)

    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.2 - 2026-05-18\n\n"
        "- mutated release notes\n"
    )
    _git(repo_path, "add", "CHANGELOG.alpha.md")
    _git(repo_path, "commit", "-m", "mutate release metadata")

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(report),
            expected_sha=None,
        )
    )

    assert result == 1  # nosec B101
    captured = capsys.readouterr()
    assert "checkout-mismatch: app checkout HEAD" in captured.err  # nosec B101
    assert f"does not match expected {expected_sha}" in captured.err  # nosec B101


def test_prerelease_publish_refuses_dirty_checkout_before_metadata_read(
    tmp_path: Path, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    (repo_path / "CHANGELOG.alpha.md").write_text("uncommitted release drift\n")

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(report),
            expected_sha=None,
        )
    )

    assert result == 1  # nosec B101
    assert (
        "checkout-mismatch: app checkout is dirty before release publish"
        in capsys.readouterr().err
    )  # nosec B101


def test_prerelease_publish_requires_control_report_or_expected_sha(
    tmp_path: Path, capsys
) -> None:
    manifest, repo_path, _expected_sha = _write_alpha_prerelease_repo(tmp_path)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(tmp_path / "missing-control-report.json"),
            expected_sha=None,
        )
    )

    assert result == 1  # nosec B101
    captured = capsys.readouterr()
    assert "unable to read control report" in captured.err  # nosec B101
    assert "expected SHA is required" in captured.err  # nosec B101


def test_prerelease_publish_allows_reset_checkout_with_matching_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha Changelog\n\n"
        "## 0.7.1-alpha.7-aio.2 - 2026-05-18\n\n"
        "- transient release drift\n"
    )
    _git(repo_path, "add", "CHANGELOG.alpha.md")
    _git(repo_path, "commit", "-m", "transient release drift")
    _git(repo_path, "reset", "--hard", expected_sha)
    _git(repo_path, "clean", "-ffd")

    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(report),
            expected_sha=None,
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create sure-alpha/0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert f"--target {expected_sha}" in output  # nosec B101


def test_prerelease_publish_create_targets_attested_head_after_followup_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, release_sha = _write_alpha_prerelease_repo(tmp_path)
    (repo_path / "app.py").write_text("print('safe')\n")
    _git(repo_path, "add", "app.py")
    _git(repo_path, "commit", "-m", "fix(runtime): remove unsafe code before publish")
    head_sha = _git(repo_path, "rev-parse", "HEAD")
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=head_sha)

    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(report),
            expected_sha=head_sha,
        )
    )

    assert result == 0  # nosec B101
    assert release_sha != head_sha  # nosec B101
    output = capsys.readouterr().out
    assert f"--target {head_sha}" in output  # nosec B101
    assert f"--target {release_sha}" not in output  # nosec B101


def test_prerelease_publish_skips_matching_github_prerelease(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            assert env["GH_TOKEN"] == "release-token"  # nosec B101
            assert any("isLatest" in part for part in command)  # nosec B101
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "targetCommitish": expected_sha,
                        "name": "0.7.1-alpha.7-aio.1",
                        "body": "- alpha release notes",
                        "isPrerelease": True,
                        "isLatest": False,
                    }
                ),
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=expected_sha,
        )
    )

    assert result == 0  # nosec B101
    assert (
        "sure-aio:sure-alpha: prerelease=already-present "
        "sure-alpha/0.7.1-alpha.7-aio.1" in capsys.readouterr().out
    )  # nosec B101


def test_prerelease_publish_falls_back_without_is_latest_field(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run
    view_commands: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            view_commands.append(command)
            if len(view_commands) == 1:
                assert command[-1] == (  # nosec B101
                    "targetCommitish,name,body,isPrerelease,isLatest"
                )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr='Unknown JSON field: "isLatest"',
                )
            assert command[-1] == "targetCommitish,name,body,isPrerelease"  # nosec B101
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "targetCommitish": expected_sha,
                        "name": "0.7.1-alpha.7-aio.1",
                        "body": "- alpha release notes",
                        "isPrerelease": True,
                    }
                ),
                stderr="",
            )
        if command[:3] in (["gh", "release", "create"], ["gh", "release", "edit"]):
            raise AssertionError(command)
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=expected_sha,
        )
    )

    assert result == 0  # nosec B101
    assert len(view_commands) == 2  # nosec B101
    assert (
        "sure-aio:sure-alpha: prerelease=already-present "
        "sure-alpha/0.7.1-alpha.7-aio.1" in capsys.readouterr().out
    )  # nosec B101


def test_prerelease_publish_targets_registry_only_sync_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, _release_sha = _write_alpha_prerelease_repo(tmp_path)
    _git(repo_path, "commit", "--amend", "-m", "chore(sync): bump sure alpha")
    expected_sha = _git(repo_path, "rev-parse", "HEAD")
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            assert env["GH_TOKEN"] == "release-token"  # nosec B101
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=True,
            control_report_json=str(report),
            expected_sha=expected_sha,
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "gh release create sure-alpha/0.7.1-alpha.7-aio.1" in output  # nosec B101
    assert f"--target {expected_sha}" in output  # nosec B101


def test_prerelease_publish_treats_existing_release_create_conflict_as_present(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run
    view_calls = 0

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        nonlocal view_calls
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            view_calls += 1
            if view_calls == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="not found")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "targetCommitish": expected_sha,
                        "name": "0.7.1-alpha.7-aio.1",
                        "body": "- alpha release notes",
                        "isPrerelease": True,
                    }
                ),
                stderr="",
            )
        if command[:3] == ["gh", "release", "create"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="HTTP 422: Validation Failed (Release.tag_name already exists)",
            )
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=expected_sha,
        )
    )

    assert result == 0  # nosec B101
    assert view_calls == 2  # nosec B101
    assert (
        "sure-aio:sure-alpha: prerelease=already-present "
        "sure-alpha/0.7.1-alpha.7-aio.1" in capsys.readouterr().out
    )  # nosec B101


def test_prerelease_publish_keeps_existing_release_target_after_followup_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, release_sha = _write_alpha_prerelease_repo(tmp_path)
    (repo_path / ".aio-fleet.yml").write_text("release: checked\n")
    _git(repo_path, "add", ".aio-fleet.yml")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile manifest")
    head_sha = _git(repo_path, "rev-parse", "HEAD")
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=head_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "targetCommitish": release_sha,
                        "name": "0.7.1-alpha.7-aio.1",
                        "body": "- alpha release notes",
                        "isPrerelease": True,
                    }
                ),
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=head_sha,
        )
    )

    assert result == 0  # nosec B101
    assert release_sha != head_sha  # nosec B101
    assert (
        "sure-aio:sure-alpha: prerelease=already-present "
        "sure-alpha/0.7.1-alpha.7-aio.1" in capsys.readouterr().out
    )  # nosec B101


def test_prerelease_publish_accepts_existing_head_target_after_followup_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, release_sha = _write_alpha_prerelease_repo(tmp_path)
    (repo_path / ".aio-fleet.yml").write_text("release: checked\n")
    _git(repo_path, "add", ".aio-fleet.yml")
    _git(repo_path, "commit", "-m", "chore(fleet): reconcile manifest")
    head_sha = _git(repo_path, "rev-parse", "HEAD")
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=head_sha)
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    real_run = cli._run

    def fake_run(command: list[str], cwd: Path | None = None, env=None):
        if command[:2] in (["git", "rev-parse"], ["git", "status"]):
            return real_run(command, cwd=cwd, env=env)
        if command[:3] == ["gh", "release", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "targetCommitish": head_sha,
                        "name": "0.7.1-alpha.7-aio.1",
                        "body": "- alpha release notes",
                        "isPrerelease": True,
                    }
                ),
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=head_sha,
        )
    )

    assert result == 0  # nosec B101
    assert release_sha != head_sha  # nosec B101
    assert (
        "sure-aio:sure-alpha: prerelease=already-present "
        "sure-alpha/0.7.1-alpha.7-aio.1" in capsys.readouterr().out
    )  # nosec B101


def test_prerelease_publish_preflights_release_credentials(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path, expected_sha = _write_alpha_prerelease_repo(tmp_path)
    report = tmp_path / "control-report.json"
    _write_control_report(report, sha=expected_sha)
    for key in (
        "AIO_FLEET_RELEASE_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check-token")

    result = cmd_release_publish_github_prereleases(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component=["sure-alpha"],
            repo_path=str(repo_path),
            dry_run=False,
            control_report_json=str(report),
            expected_sha=expected_sha,
        )
    )

    assert result == 1  # nosec B101
    captured = capsys.readouterr()
    assert "credential-gap: missing" in captured.err  # nosec B101


def test_latest_main_ci_requires_external_id_bound_check(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    repo = load_manifest(manifest).repo("example-aio")
    sha = "a" * 40
    expected_external_id = cli.check_external_id(repo, sha=sha, event="push")

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        del cwd
        if "commits/main" in command[2]:
            return SimpleNamespace(returncode=0, stdout=f"{sha}\n", stderr="")
        if "check-runs" in command[2]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "check_runs": [
                            {
                                "external_id": "attacker-controlled",
                                "status": "completed",
                                "conclusion": "success",
                            },
                            {
                                "external_id": expected_external_id,
                                "status": "completed",
                                "conclusion": "failure",
                            },
                        ]
                    }
                ),
                stderr="",
            )
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cli._latest_main_ci(repo)

    assert result["state"] == "failure"  # nosec B101


def test_release_version_catches_changelog_system_exit(
    tmp_path: Path, monkeypatch
) -> None:
    _manifest, _repo_path = _write_minimal_manifest(tmp_path)
    repo = load_manifest(_manifest).repo("example-aio")

    monkeypatch.setattr(
        cli,
        "latest_changelog_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("bad changelog")),
    )

    assert cli._release_version(repo) == ""  # nosec B101


def test_release_readiness_outputs_component_operator_commands(
    tmp_path: Path, monkeypatch, capsys
) -> None:
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
    github_repo: wgross19/sure-aio
    publish_profile: upstream-aio-track
    components:
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
        release_changelog: CHANGELOG.alpha.md
        floating_tags:
          - latest-alpha
""")
    (repo_path / "CHANGELOG.alpha.md").write_text(
        "# Alpha\n\n## 0.7.1-alpha.7-aio.6 - 2026-05-18\n\n- alpha\n"
    )
    sha = "d" * 40

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        del cwd
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "rev-list"]:
            return SimpleNamespace(returncode=0, stdout="0 0\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_open_prs", lambda _repo: "0")
    monkeypatch.setattr(cli, "load_policy", lambda _path: {"repositories": []})
    monkeypatch.setattr(cli, "_latest_main_ci", lambda _repo: {"state": "success"})
    monkeypatch.setattr(cli, "_image_status", lambda _repo, *, component="aio": "ok")
    monkeypatch.setattr(cli, "_git_head", lambda _path: sha)

    result = cmd_release_readiness(
        Namespace(
            manifest=str(manifest),
            repo="sure-aio",
            component="sure-alpha",
            catalog_path=None,
            policy="unused.yml",
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "sure-aio:sure-alpha: release-readiness=ready" in output  # nosec B101
    assert (  # nosec B101
        f"uv run aio-fleet registry verify --repo sure-aio --component sure-alpha --sha {sha} --verbose"
        in output
    )
    assert (  # nosec B101
        "uv run aio-fleet registry publish --repo sure-aio --component sure-alpha"
        not in output
    )
    assert (  # nosec B101
        "uv run aio-fleet release publish --repo sure-aio --component sure-alpha"
        in output
    )
    assert (  # nosec B101
        f"uv run aio-fleet control-check --repo sure-aio --sha {sha} --event push --publish --publish-component sure-alpha"
        in output
    )


def test_debt_report_text_prints_publish_state_once(
    tmp_path: Path, monkeypatch, capsys
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
""")

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "rev-list"]:
            return SimpleNamespace(returncode=0, stdout="0 0\n", stderr="")
        if command[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_debt_report(
        Namespace(
            manifest=str(manifest),
            catalog_path=None,
            github=False,
            policy="unused.yml",
            trunk=False,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "publish=source-ready" in output  # nosec B101
    assert "publish=publish=" not in output  # nosec B101


def test_registry_publish_verifies_with_repo_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.delenv("AIO_FLEET_GHCR_TOKEN", raising=False)

    def fake_run(
        command: list[str], cwd: Path | None = None, env=None
    ) -> SimpleNamespace:
        seen["publish_command"] = command
        seen["publish_cwd"] = cwd
        seen["publish_env"] = env
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_verify(tags: list[str], *, env=None) -> list[str]:
        seen.setdefault("verify_calls", []).append({"tags": tags, "env": env})
        return []

    monkeypatch.setattr(cli, "_run_streaming", fake_run)
    monkeypatch.setattr(cli, "verify_registry_tags", fake_verify)

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="aio",
            dry_run=False,
            force=True,
        )
    )

    assert result == 0  # nosec B101
    assert seen["publish_cwd"] == repo_path.resolve()  # nosec B101
    assert seen["publish_env"] is None  # nosec B101
    verify_calls = seen["verify_calls"]
    assert len(verify_calls) == 1  # nosec B101
    assert verify_calls[0]["env"] is None  # nosec B101
    assert any(
        "sha-" + "a" * 40 in tag for tag in verify_calls[0]["tags"]
    )  # nosec B101
    captured = capsys.readouterr()
    assert "example-aio:aio: registry=publishing" in captured.out  # nosec B101
    assert "preflight" not in captured.err  # nosec B101


def test_registry_publish_skips_when_tags_are_current(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.delenv("AIO_FLEET_GHCR_TOKEN", raising=False)

    monkeypatch.setattr(
        cli,
        "_run_streaming",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("publish should be skipped")
        ),
    )
    monkeypatch.setattr(cli, "verify_registry_tags", lambda _tags, **_kwargs: [])
    monkeypatch.setattr(cli, "_registry_tags_match_sha_digest", lambda _tags: True)

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="aio",
            dry_run=False,
            force=False,
        )
    )

    assert result == 0  # nosec B101
    assert (
        "example-aio: registry=already-present" in capsys.readouterr().out
    )  # nosec B101


def test_registry_publish_fails_when_preserved_tag_changes(
    tmp_path: Path, monkeypatch, capsys
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
      sure-alpha:
        image_name: wgross19/example-aio
        dockerfile: Dockerfile.alpha
        floating_tags:
          - latest-alpha
        sha_tag_prefix: sha-alpha-
        preserve_tags:
          - wgross19/example-aio:latest
""")
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.delenv("AIO_FLEET_GHCR_TOKEN", raising=False)
    monkeypatch.setattr(
        cli, "_run_streaming", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(cli, "verify_registry_tags", lambda _tags, **_kwargs: [])
    digests = iter(["sha256:before", "sha256:after"])
    monkeypatch.setattr(
        cli, "_registry_tag_digest", lambda *_args, **_kwargs: next(digests)
    )

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="sure-alpha",
            dry_run=False,
            force=True,
        )
    )

    assert result == 1  # nosec B101
    captured = capsys.readouterr()
    assert "protected digest changed" in captured.err  # nosec B101


def test_registry_publish_refuses_template_profile(tmp_path: Path, capsys) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  unraid-aio-template:
    path: {repo_path}
    public: true
    app_slug: unraid-aio-template
    image_name: wgross19/unraid-aio-template
    docker_cache_scope: unraid-aio-template-image
    pytest_image_tag: unraid-aio-template:pytest
    publish_profile: template
""")

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="unraid-aio-template",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="aio",
            dry_run=False,
            force=False,
        )
    )

    assert result == 1  # nosec B101
    assert (
        "unraid-aio-template: registry publish is disabled for template-profile repos"
        in capsys.readouterr().err
    )  # nosec B101


def test_registry_delete_dockerhub_tags_dry_run_without_credentials(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)

    result = cmd_registry_delete_dockerhub_tags(
        Namespace(
            image="wgross19/sure-aio",
            tag=["latest-alpha"],
            tag_list="0.7.1-alpha.7-aio.4",
            required_substring="alpha",
            dry_run=True,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    assert capsys.readouterr().out.splitlines() == [  # nosec B101
        "wgross19/sure-aio:latest-alpha: would-delete",
        "wgross19/sure-aio:0.7.1-alpha.7-aio.4: would-delete",
    ]


def test_registry_delete_dockerhub_tags_prefers_delete_token(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    seen: dict[str, str] = {}

    def fake_delete(**kwargs):
        seen["token"] = kwargs["token"]
        return [{"tag": "latest-alpha", "state": "deleted"}]

    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "publish-token")
    monkeypatch.setenv("DOCKERHUB_DELETE_TOKEN", "delete-token")
    monkeypatch.setattr(cli, "delete_dockerhub_tags", fake_delete)

    result = cmd_registry_delete_dockerhub_tags(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            component="aio",
            image=None,
            tag=["latest-alpha"],
            tag_list="",
            required_substring="alpha",
            dry_run=False,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    assert seen["token"] == "delete-token"  # nosec B101
    assert (
        "wgross19/example-aio:latest-alpha: deleted" in capsys.readouterr().out
    )  # nosec B101


def test_registry_delete_dockerhub_tags_rejects_live_freeform_image(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_DELETE_TOKEN", "delete-token")

    result = cmd_registry_delete_dockerhub_tags(
        Namespace(
            manifest=str(manifest),
            repo=None,
            repo_path=None,
            component="aio",
            image="wgross19/not-in-fleet",
            tag=["latest-alpha"],
            tag_list="",
            required_substring="alpha",
            dry_run=False,
            format="text",
        )
    )

    assert result == 1  # nosec B101
    assert "manifest repo/component" in capsys.readouterr().err  # nosec B101


def test_registry_delete_dockerhub_tags_rejects_manifest_image_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_DELETE_TOKEN", "delete-token")

    result = cmd_registry_delete_dockerhub_tags(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            component="aio",
            image="wgross19/not-in-fleet",
            tag=["latest-alpha"],
            tag_list="",
            required_substring="alpha",
            dry_run=False,
            format="text",
        )
    )

    assert result == 1  # nosec B101
    assert "does not match manifest target" in capsys.readouterr().err  # nosec B101


def test_registry_preflight_reports_publish_credential_gaps(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.delenv("DOCKERHUB_USERNAME", raising=False)
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.delenv("AIO_FLEET_GHCR_TOKEN", raising=False)

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["publish"],
            repo=None,
            repo_path=None,
            component="aio",
            image=None,
            live_auth=False,
            check_delete_scope=False,
            allow_publish_token_delete_fallback=False,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"  # nosec B101
    assert "DOCKERHUB_USERNAME" in report["checks"][0]["detail"]  # nosec B101
    assert "AIO_FLEET_GHCR_TOKEN" in report["checks"][0]["detail"]  # nosec B101


def test_registry_preflight_checks_live_dockerhub_auth(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    seen: dict[str, str] = {}

    def fake_auth(*, username: str, token: str) -> str | None:
        seen["username"] = username
        seen["token"] = token
        return None

    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "publish-token")
    monkeypatch.setenv("AIO_FLEET_GHCR_TOKEN", "ghcr-token")
    monkeypatch.setattr(cli, "dockerhub_auth_preflight_failure", fake_auth)

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["publish"],
            repo=None,
            repo_path=None,
            component="aio",
            image=None,
            live_auth=True,
            check_delete_scope=False,
            allow_publish_token_delete_fallback=False,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    assert seen == {"username": "wgross19", "token": "publish-token"}  # nosec B101
    assert "dockerhub-publish-auth: ok" in capsys.readouterr().out  # nosec B101


def test_registry_preflight_accepts_preauthenticated_docker_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    monkeypatch.setenv("AIO_FLEET_REGISTRY_AUTH_MODE", "preauthenticated")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.delenv("DOCKERHUB_TOKEN", raising=False)
    monkeypatch.delenv("AIO_FLEET_GHCR_TOKEN", raising=False)

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["publish"],
            repo=None,
            repo_path=None,
            component="aio",
            image=None,
            live_auth=True,
            check_delete_scope=False,
            allow_publish_token_delete_fallback=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"  # nosec B101
    assert report["checks"][0]["detail"] == (  # nosec B101
        "preauthenticated Docker config is present"
    )


def test_registry_preflight_cleanup_requires_delete_token(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "publish-token")
    monkeypatch.delenv("DOCKERHUB_DELETE_TOKEN", raising=False)

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["cleanup"],
            repo=None,
            repo_path=None,
            component="aio",
            image="wgross19/sure-aio-alpha",
            live_auth=False,
            check_delete_scope=False,
            allow_publish_token_delete_fallback=False,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][0]["name"] == "cleanup-credentials"  # nosec B101
    assert "DOCKERHUB_DELETE_TOKEN" in report["checks"][0]["detail"]  # nosec B101


def test_registry_preflight_cleanup_ignores_publish_token_fallback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)
    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "publish-token")
    monkeypatch.delenv("DOCKERHUB_DELETE_TOKEN", raising=False)

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["cleanup"],
            repo=None,
            repo_path=None,
            component="aio",
            image="wgross19/example-aio",
            live_auth=False,
            check_delete_scope=False,
            allow_publish_token_delete_fallback=True,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][0]["name"] == "cleanup-credentials"  # nosec B101
    assert "DOCKERHUB_DELETE_TOKEN" in report["checks"][0]["detail"]  # nosec B101
    assert "DOCKERHUB_TOKEN" not in report["checks"][0]["detail"]  # nosec B101


def test_registry_preflight_delete_scope_reports_forbidden(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_DELETE_TOKEN", "delete-token")
    monkeypatch.setattr(cli, "dockerhub_auth_preflight_failure", lambda **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "dockerhub_delete_scope_preflight_failure",
        lambda **_kwargs: "wgross19/sure-aio-alpha: Docker Hub delete forbidden",
    )

    result = cmd_registry_preflight(
        Namespace(
            manifest=str(manifest),
            mode=["cleanup"],
            repo=None,
            repo_path=None,
            component="aio",
            image="wgross19/sure-aio-alpha",
            live_auth=True,
            check_delete_scope=True,
            allow_publish_token_delete_fallback=False,
            format="json",
        )
    )

    assert result == 1  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][-1]["name"] == "dockerhub-delete-scope"  # nosec B101
    assert report["checks"][-1]["status"] == "failed"  # nosec B101
    assert "delete forbidden" in report["checks"][-1]["detail"]  # nosec B101


def test_registry_publish_logs_in_with_temporary_scrubbed_docker_config(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    seen: dict[str, object] = {"login_commands": [], "buildx_commands": []}

    def fake_docker(command: list[str], **kwargs: object):
        docker_env = kwargs["env"]
        assert isinstance(docker_env, dict)  # nosec B101
        assert "DOCKER_CONFIG" in docker_env  # nosec B101
        assert "DOCKERHUB_TOKEN" not in docker_env  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in docker_env  # nosec B101
        if command[:2] == ["docker", "login"]:
            assert kwargs["input"] in {"hub-token\n", "ghcr-token\n"}  # nosec B101
            seen["login_commands"].append(command)  # type: ignore[union-attr]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["docker", "buildx"]:
            seen["buildx_commands"].append(command)  # type: ignore[union-attr]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker command: {command}")

    def fake_publish(command: list[str], cwd: Path | None = None, env=None):
        seen["publish_command"] = command
        seen["publish_cwd"] = cwd
        seen["publish_env"] = env
        assert isinstance(env, dict)  # nosec B101
        assert "BUILDX_BUILDER" in env  # nosec B101
        assert "DOCKER_CONFIG" in env  # nosec B101
        assert "DOCKERHUB_TOKEN" not in env  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in env  # nosec B101
        assert "GH_TOKEN" not in env  # nosec B101
        assert "GITHUB_TOKEN" not in env  # nosec B101
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("DOCKERHUB_USERNAME", "wgross19")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "hub-token")
    monkeypatch.setenv("AIO_FLEET_GHCR_TOKEN", "ghcr-token")
    monkeypatch.setenv("AIO_FLEET_GHCR_USERNAME", "wgross19")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setattr(cli.subprocess, "run", fake_docker)
    monkeypatch.setattr(cli, "_run_streaming", fake_publish)

    def fake_verify(_tags: list[str], *, env=None) -> list[str]:
        seen["verify_env"] = env
        assert isinstance(env, dict)  # nosec B101
        assert "BUILDX_BUILDER" in env  # nosec B101
        assert "DOCKER_CONFIG" in env  # nosec B101
        assert "DOCKERHUB_TOKEN" not in env  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in env  # nosec B101
        assert "GH_TOKEN" not in env  # nosec B101
        assert "GITHUB_TOKEN" not in env  # nosec B101
        return []

    monkeypatch.setattr(cli, "verify_registry_tags", fake_verify)

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="aio",
            dry_run=False,
            force=True,
        )
    )

    assert result == 0  # nosec B101
    assert seen["login_commands"] == [  # nosec B101
        [
            "docker",
            "login",
            "docker.io",
            "--username",
            "wgross19",
            "--password-stdin",
        ],
        [
            "docker",
            "login",
            "ghcr.io",
            "--username",
            "wgross19",
            "--password-stdin",
        ],
    ]
    buildx_commands = seen["buildx_commands"]
    assert [command[:3] for command in buildx_commands] == [  # nosec B101
        ["docker", "buildx", "create"],
        ["docker", "buildx", "inspect"],
        ["docker", "buildx", "rm"],
    ]
    assert seen["publish_cwd"] == repo_path.resolve()  # nosec B101
    assert seen["verify_env"] == seen["publish_env"]  # nosec B101


def test_registry_publish_can_use_preauthenticated_docker_config(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    seen: dict[str, object] = {"buildx_commands": []}

    def fake_docker(command: list[str], **kwargs: object):
        assert command[:2] != ["docker", "login"]  # nosec B101
        docker_env = kwargs["env"]
        assert isinstance(docker_env, dict)  # nosec B101
        assert docker_env["DOCKER_CONFIG"] == str(docker_config)  # nosec B101
        assert "DOCKERHUB_TOKEN" not in docker_env  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in docker_env  # nosec B101
        if command[:2] == ["docker", "buildx"]:
            seen["buildx_commands"].append(command)  # type: ignore[union-attr]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker command: {command}")

    def fake_publish(command: list[str], cwd: Path | None = None, env=None):
        seen["publish_command"] = command
        seen["publish_cwd"] = cwd
        seen["publish_env"] = env
        assert isinstance(env, dict)  # nosec B101
        assert env["DOCKER_CONFIG"] == str(docker_config)  # nosec B101
        assert "DOCKERHUB_TOKEN" not in env  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in env  # nosec B101
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("AIO_FLEET_REGISTRY_AUTH_MODE", "preauthenticated")
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    monkeypatch.setenv("DOCKERHUB_TOKEN", "hub-token")
    monkeypatch.setenv("AIO_FLEET_GHCR_TOKEN", "ghcr-token")
    monkeypatch.setattr(cli.subprocess, "run", fake_docker)
    monkeypatch.setattr(cli, "_run_streaming", fake_publish)
    monkeypatch.setattr(cli, "verify_registry_tags", lambda _tags, *, env=None: [])

    result = cmd_registry_publish(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=str(repo_path),
            sha="a" * 40,
            component="aio",
            dry_run=False,
            force=True,
        )
    )

    assert result == 0  # nosec B101
    buildx_commands = seen["buildx_commands"]
    assert [command[:3] for command in buildx_commands] == [  # nosec B101
        ["docker", "buildx", "create"],
        ["docker", "buildx", "inspect"],
        ["docker", "buildx", "rm"],
    ]
    assert seen["publish_cwd"] == repo_path.resolve()  # nosec B101


def test_registry_verify_all_skips_manual_template_publish(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    template_path = tmp_path / "template"
    app_path = tmp_path / "app"
    template_path.mkdir()
    app_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  unraid-aio-template:
    path: {template_path}
    public: true
    app_slug: unraid-aio-template
    image_name: wgross19/unraid-aio-template
    docker_cache_scope: unraid-aio-template-image
    pytest_image_tag: aio-template:pytest
    publish_profile: template
  example-aio:
    path: {app_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    verified: list[str] = []

    monkeypatch.setattr(cli, "_git_head", lambda _path: "a" * 40)

    def fake_verify(tags: list[str], **_kwargs) -> list[str]:
        verified.extend(tags)
        return []

    monkeypatch.setattr(cli, "verify_registry_tags", fake_verify)

    result = cmd_registry_verify(
        Namespace(
            manifest=str(manifest),
            all=True,
            repo=None,
            repo_path=None,
            sha=None,
            component="aio",
            include_manual=False,
            dry_run=False,
            format="json",
            verbose=False,
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["repos"][0]["repo"] == "unraid-aio-template"  # nosec B101
    assert report["repos"][0]["skipped"] == "manual-template-publish"  # nosec B101
    assert all("unraid-aio-template" not in tag for tag in verified)  # nosec B101
    assert any("example-aio" in tag for tag in verified)  # nosec B101


def test_registry_verify_reports_skipped_sha_tag_for_metadata_only_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    app_path = tmp_path / "example-aio"
    app_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {app_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    verified: list[str] = []
    sha = "a" * 40

    monkeypatch.setattr(cli, "_git_head", lambda _path: sha)
    monkeypatch.setattr(
        cli, "registry_sha_tag_required", lambda *_args, **_kwargs: False
    )

    def fake_compute_tags(*_args, **kwargs) -> RegistryTagSet:
        assert kwargs["include_sha_tag"] is False  # nosec B101
        return RegistryTagSet(
            dockerhub=["wgross19/example-aio:latest"],
            ghcr=["ghcr.io/wgross19/example-aio:latest"],
            upstream_version="1.0.0",
            release_package_tag="1.0.0-aio.1",
        )

    def fake_verify(tags: list[str], **_kwargs) -> list[str]:
        verified.extend(tags)
        return []

    monkeypatch.setattr(cli, "compute_registry_tags", fake_compute_tags)
    monkeypatch.setattr(cli, "verify_registry_tags", fake_verify)

    result = cmd_registry_verify(
        Namespace(
            manifest=str(manifest),
            all=True,
            repo=None,
            repo_path=None,
            sha=None,
            component="aio",
            include_manual=False,
            dry_run=False,
            format="json",
            verbose=False,
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["repos"][0]["sha_tag"] == "skipped"  # nosec B101
    assert f"sha-{sha}" not in " ".join(verified)  # nosec B101


def test_debt_report_flags_repos_missing_from_github_policy(
    tmp_path: Path, monkeypatch, capsys
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
""")
    policy = tmp_path / "github-policy.yml"
    policy.write_text("repositories: {}\n")

    def fake_run(command: list[str], cwd: Path | None = None) -> SimpleNamespace:
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "rev-list"]:
            return SimpleNamespace(returncode=0, stdout="0 0\n", stderr="")
        if command[:2] == ["git", "ls-files"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(cli, "_run", fake_run)

    result = cmd_debt_report(
        Namespace(
            manifest=str(manifest),
            catalog_path=None,
            github=True,
            policy=str(policy),
            trunk=False,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    report = json.loads(capsys.readouterr().out)
    assert report["repos"][0]["github_policy_failures"] == [  # nosec B101
        "example-aio: missing from github policy"
    ]
    assert report["repos"][0]["publish"] == "publish=blocked:policy"  # nosec B101


def test_validate_template_common_accepts_manifest_repo(tmp_path: Path, capsys) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example defaults and advanced settings for operators.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText/>
  <DonateLink/>
  <Changes>### 2026-01-01
- Generated from CHANGELOG.md during release preparation. Do not edit manually.
- Initial release.</Changes>
</Container>
""")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
awesome_unraid_repository: wgross19/awesome-unraid
repos:
  example-aio:
    path: {repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
    catalog_assets:
      - source: example-aio.xml
        target: example-aio.xml
""")

    result = cmd_validate_template_common(
        Namespace(manifest=str(manifest), repo="example-aio", repo_path=None, all=False)
    )

    assert result == 0  # nosec B101
    assert "common template validation passed" in capsys.readouterr().out  # nosec B101


def test_validate_all_includes_manifest_shape_failures(tmp_path: Path, capsys) -> None:
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
    catalog_assets: []
""")

    result = cmd_validate(Namespace(manifest=str(manifest), all=True, repo=None))

    assert result == 1  # nosec B101
    assert "example-aio: missing Dockerfile" in capsys.readouterr().err  # nosec B101


def test_catalog_pr_body_rejects_non_public_repo_text() -> None:
    with pytest.raises(ValueError, match="catalog PR body"):
        cli._catalog_body(  # noqa: SLF001
            "/Users/shadowbook/Documents/example-aio",
            icon_only=False,
        )


def test_infra_doctor_checks_local_policy_without_tofu(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    git = shutil.which("git")
    assert git  # nosec B101
    subprocess.run(
        [git, "init"], cwd=tmp_path, check=True, capture_output=True
    )  # nosec B603
    (tmp_path / ".gitignore").write_text(
        "infra/github/*.tfstate\ninfra/github/*.tfstate.*\ninfra/github/*.tfvars\n"
    )
    infra = tmp_path / "infra" / "github"
    infra.mkdir(parents=True)
    (infra / ".terraform.lock.hcl").write_text("# lock\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {tmp_path / "example-aio"}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    policy = infra / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  actions:
    patterns_allowed:
      - wgross19/aio-fleet/.github/workflows/aio-*.yml@*
repositories:
  aio-fleet: {}
  awesome-unraid: {}
  example-aio: {}
""")

    monkeypatch.chdir(tmp_path)

    result = cmd_infra_doctor(
        Namespace(
            manifest=str(manifest),
            path=str(infra),
            policy=str(policy),
            skip_tofu=True,
        )
    )

    assert result == 0  # nosec B101
    assert "infra doctor passed" in capsys.readouterr().out  # nosec B101


def test_onboard_repo_renders_manifest_skeleton(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="example-aio",
            profile="changelog-version",
            image_name=None,
            upstream_name="Example",
            local_path_base="<local-checkout-path>",
            format="text",
            dry_run=True,
            mode="existing",
            shape=None,
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "example-aio:" in output  # nosec B101
    assert "path: <local-checkout-path>/example-aio" in output  # nosec B101
    assert (  # nosec B101
        "uv run aio-fleet export-app-manifest --repo example-aio --write" in output
    )


def test_onboard_repo_rehab_mode_outputs_checklist(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="nanoclaw-aio",
            profile="changelog-version",
            image_name=None,
            upstream_name="NanoClaw",
            local_path_base="/Users/shadowbook/Documents",
            format="json",
            dry_run=True,
            mode="rehab",
            shape=None,
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "rehab"  # nosec B101
    assert "local repo synced to main" in payload["acceptance_checklist"]  # nosec B101
    assert any(
        "fetch --prune" in item for item in payload["first_commands"]
    )  # nosec B101


def test_onboard_repo_new_from_template_outputs_creation_steps(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="future-aio",
            profile="upstream-aio-track",
            image_name=None,
            upstream_name="Future",
            local_path_base="/Users/shadowbook/Documents",
            format="json",
            dry_run=True,
            mode="new-from-template",
            shape=None,
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "new-from-template"  # nosec B101
    assert any(
        "unraid-aio-template" in step for step in payload["creation_steps"]
    )  # nosec B101
    assert payload["first_commands"][0].startswith("gh repo create")  # nosec B101


def test_onboard_repo_multi_component_shape_outputs_nanoclaw_pack(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="nanoclaw-aio",
            profile="changelog-version",
            image_name="wgross19/nanoclaw-aio",
            upstream_name="NanoClaw",
            local_path_base="/Users/shadowbook/Documents",
            format="json",
            dry_run=True,
            mode="existing",
            shape="multi-component",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["shape"] == "multi-component"  # nosec B101
    assert (
        payload["manifest_entry"]["publish_profile"] == "multi-component"
    )  # nosec B101
    assert payload["manifest_entry"]["runtime_supervisor"] == "s6"  # nosec B101
    assert payload["manifest_entry"]["runtime_healthcheck_markers"] == [  # nosec B101
        'pgrep -f "dist/index.js"'
    ]
    components = payload["manifest_entry"]["components"]
    assert components["agent"]["release_policy"] == "registry_only"  # nosec B101
    assert components["agent"]["dockerfile"] == (  # nosec B101
        "components/nanoclaw-agent/Dockerfile"
    )
    assert payload["manifest_entry"]["catalog_assets"] == [  # nosec B101
        {"source": "nanoclaw-aio.xml", "target": "nanoclaw-aio.xml"}
    ]
    assert "nanoclaw-agent.xml" not in json.dumps(payload)  # nosec B101
    assert any(
        item["component"] == "agent" and item["release_policy"] == "registry_only"
        for item in payload["component_publish"]
    )  # nosec B101
    assert any(
        "--component agent" in command for command in payload["first_commands"]
    )  # nosec B101
    assert any(
        "component-specific registry verify" in item
        for item in payload["acceptance_checklist"]
    )  # nosec B101


def test_onboard_repo_multi_component_shape_outputs_penpot_monitor_pack(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="penpot-aio",
            profile="multi-component",
            image_name="wgross19/penpot-aio",
            upstream_name="Penpot",
            local_path_base="/Users/shadowbook/Documents",
            format="json",
            dry_run=True,
            mode="existing",
            shape="multi-component",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    entry = payload["manifest_entry"]
    assert entry["publish_profile"] == "changelog-version"  # nosec B101
    assert "components" not in entry  # nosec B101
    assert {item["component"] for item in entry["upstream_monitor"]} == {  # nosec B101
        "frontend",
        "backend",
        "exporter",
        "mcp",
        "mailpit",
    }
    assert any(
        item["component"] == "frontend"
        and item["release_policy"] == "upstream_digest_only"
        for item in payload["component_publish"]
    )  # nosec B101
    assert all(
        "--component frontend" not in command for command in payload["first_commands"]
    )  # nosec B101
    assert any(
        "--component aio" in command for command in payload["first_commands"]
    )  # nosec B101


def test_onboard_repo_destination_shape_stays_dashboard_only(capsys) -> None:
    result = cmd_onboard_repo(
        Namespace(
            repo="awesome-unraid",
            profile="template",
            image_name=None,
            upstream_name="Awesome Unraid",
            local_path_base="/Users/shadowbook/Documents",
            format="json",
            dry_run=True,
            mode="existing",
            shape="destination-only",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_entry"] == {}  # nosec B101
    assert "awesome-unraid" in payload["dashboard_entry"]  # nosec B101
    assert all(
        "export-app-manifest" not in command for command in payload["first_commands"]
    )  # nosec B101


def test_upstream_monitor_dry_run_reports_updates(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.0\n")

    monkeypatch.setattr(
        cli,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
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
                updates_available=True,
                dockerfile=repo_path / "Dockerfile",
                release_notes_url="https://github.com/example/app/releases",
            )
        ],
    )

    result = cmd_upstream_monitor(
        Namespace(
            manifest=str(manifest),
            all=True,
            repo=None,
            repo_path=None,
            include_manual=False,
            write=False,
            create_pr=False,
            post_check=False,
            dry_run=True,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    assert "example-aio: upstream=updates" in capsys.readouterr().out  # nosec B101


def test_upstream_monitor_reports_blocked_submodule_ref_without_pr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.1\n")
    blocked = SimpleNamespace(
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
        updates_available=True,
        blocked=True,
        blocked_reason="missing configured submodule ref",
        next_action="create and push codex/openmemory-v2.0.2-aio",
        dockerfile=repo_path / "Dockerfile",
        release_notes_url="https://github.com/mem0ai/mem0/releases",
        submodule_path="openmemory",
        submodule_ref="codex/openmemory-v2.0.2-aio",
    )

    monkeypatch.setattr(cli, "monitor_repo", lambda *_args, **_kwargs: [blocked])
    monkeypatch.setattr(
        cli,
        "create_or_update_upstream_pr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked updates should not open a PR")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_run_generator_for_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked updates should not regenerate manifests")
        ),
    )

    result = cmd_upstream_monitor(
        Namespace(
            manifest=str(manifest),
            all=True,
            repo=None,
            repo_path=None,
            include_manual=False,
            write=True,
            create_pr=True,
            post_check=True,
            dry_run=False,
            format="text",
        )
    )

    assert result == 0  # nosec B101
    output = capsys.readouterr().out
    assert "example-aio: upstream=blocked" in output  # nosec B101
    assert "missing configured submodule ref" in output  # nosec B101


def test_upstream_write_exports_app_manifest_when_expected(tmp_path: Path) -> None:
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
    upstream_commit_paths:
      - .aio-fleet.yml
""")

    cli._run_generator_for_write(load_manifest(manifest).repo("example-aio"))

    exported = repo_path / ".aio-fleet.yml"
    assert exported.exists()  # nosec B101
    assert "repo: example-aio" in exported.read_text()  # nosec B101


def test_upstream_write_generator_strips_token_environment(
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
    generator_check_command: python -V --check
""")
    observed: dict[str, str] = {}

    def fake_run(
        command: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ):
        assert command == ["python", "-V"]  # nosec B101
        assert cwd == repo_path  # nosec B101
        assert isinstance(env, dict)  # nosec B101
        observed.update(env)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setenv("APP_TOKEN", "app")
    monkeypatch.setenv("AIO_FLEET_UPSTREAM_TOKEN", "upstream")
    monkeypatch.setenv("AIO_FLEET_WORKFLOW_TOKEN", "workflow")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check")
    monkeypatch.setenv("AIO_FLEET_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "dockerhub")
    monkeypatch.setenv("CUSTOM_WEBHOOK_URL", "webhook")
    monkeypatch.setenv("SAFE_TEST_FLAG", "present")
    monkeypatch.setenv("GH_TOKEN", "gh")
    monkeypatch.setenv("GITHUB_TOKEN", "github")

    cli._run_generator_for_write(load_manifest(manifest).repo("example-aio"))

    assert observed.get("SAFE_TEST_FLAG") == "present"  # nosec B101
    assert "aio-fleet-generator-home-" in observed.get("HOME", "")  # nosec B101
    assert "APP_TOKEN" not in observed  # nosec B101
    assert "AIO_FLEET_UPSTREAM_TOKEN" not in observed  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in observed  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in observed  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in observed  # nosec B101
    assert "DOCKERHUB_TOKEN" not in observed  # nosec B101
    assert "CUSTOM_WEBHOOK_URL" not in observed  # nosec B101
    assert "GH_TOKEN" not in observed  # nosec B101
    assert "GITHUB_TOKEN" not in observed  # nosec B101


def test_upstream_assess_outputs_json(tmp_path: Path, monkeypatch, capsys) -> None:
    manifest, _repo_path = _write_minimal_manifest(tmp_path)

    class FakeAssessment:
        safety_level = "warn"

        def to_dict(self):
            return {
                "repo": "example-aio",
                "component": "aio",
                "safety_level": "warn",
                "confidence": 0.6,
                "signals": [],
                "warnings": ["release notes mention review keyword(s): config"],
                "failures": [],
                "next_action": "release notes mention review keyword(s): config",
                "config_delta": "none",
                "template_impact": "no-xml-change",
                "runtime_smoke": "not-configured",
                "changed_files": ["Dockerfile"],
            }

    monkeypatch.setattr(
        cli, "assess_upstream_pr", lambda *_args, **_kwargs: FakeAssessment()
    )

    result = cmd_upstream_assess(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            pr=12,
            branch=None,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["safety_level"] == "warn"  # nosec B101


def test_upstream_assess_without_pr_uses_monitor_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest, repo_path = _write_minimal_manifest(tmp_path)

    class FakeAssessment:
        safety_level = "manual"

        def to_dict(self):
            return {
                "repo": "example-aio",
                "component": "aio",
                "safety_level": "manual",
                "confidence": 0.2,
                "signals": ["notify-only upstream strategy"],
                "warnings": [],
                "failures": [],
                "next_action": "manual triage required before source PR",
                "config_delta": "not-assessed",
                "template_impact": "manual",
                "runtime_smoke": "not-configured",
                "changed_files": [],
            }

    monitor_results = [
        SimpleNamespace(
            repo="example-aio",
            component="aio",
            updates_available=True,
            strategy="notify",
        )
    ]
    monkeypatch.setattr(cli, "monitor_repo", lambda *_args, **_kwargs: monitor_results)

    def fake_assess(repo, results, *, changed_files):
        assert repo.path == repo_path  # nosec B101
        assert results == monitor_results  # nosec B101
        assert changed_files == []  # nosec B101
        return FakeAssessment()

    monkeypatch.setattr(cli, "assess_expected_update", fake_assess)

    result = cmd_upstream_assess(
        Namespace(
            manifest=str(manifest),
            repo="example-aio",
            repo_path=None,
            pr=None,
            branch=None,
            format="json",
        )
    )

    assert result == 0  # nosec B101
    payload = json.loads(capsys.readouterr().out)
    assert payload["safety_level"] == "manual"  # nosec B101
