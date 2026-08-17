from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path

from aio_fleet import signing
from aio_fleet.manifest import load_manifest
from aio_fleet.signing import (
    SigningTarget,
    signing_doctor_report,
    workflow_writer_checks,
)


def test_signing_doctor_reports_missing_app_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = _repo(tmp_path / "example-aio")
    manifest = _manifest(tmp_path, repo_path)

    monkeypatch.setattr(signing, "_gh_json", _ok_gh_json)

    report = signing_doctor_report(load_manifest(manifest), env={}, include_hooks=False)

    assert report["status"] == "failed"  # nosec B101
    assert "credential-gap" in report["failure_classes"]  # nosec B101
    assert any(  # nosec B101
        check["name"] == "fleetbot-credentials" and check["class"] == "credential-gap"
        for check in report["checks"]
    )


def test_signing_doctor_checks_destination_targets_when_selected(
    tmp_path: Path, monkeypatch
) -> None:
    source = _repo(tmp_path / "example-aio")
    catalog = _repo(tmp_path / "awesome-unraid")
    manifest = _manifest(tmp_path, source, catalog_path=catalog)
    seen: list[str] = []

    def fake_gh_json(command: list[str], *, check: bool):  # noqa: ARG001
        if command[0] == "api" and command[1].endswith("/protection"):
            seen.append(command[1])
            return {"required_signatures": {"enabled": True}}
        if command[:2] == ["pr", "list"]:
            return []
        raise AssertionError(command)

    monkeypatch.setattr(signing, "_gh_json", fake_gh_json)

    report = signing_doctor_report(
        load_manifest(manifest),
        repos=["awesome-unraid"],
        env=_app_env(),
        include_hooks=False,
    )

    assert report["status"] == "ok"  # nosec B101
    assert seen == [  # nosec B101
        "repos/wgross19/awesome-unraid/branches/main/protection"
    ]


def test_signing_doctor_accepts_legacy_app_id_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = _repo(tmp_path / "example-aio")
    manifest = _manifest(tmp_path, repo_path)
    env = {
        "AIO_FLEET_APP_ID": "123",
        "AIO_FLEET_APP_INSTALLATION_ID": "456",
        "AIO_FLEET_APP_PRIVATE_KEY": "private-key",
    }

    monkeypatch.setattr(signing, "_gh_json", _ok_gh_json)

    report = signing_doctor_report(
        load_manifest(manifest), env=env, include_hooks=False
    )

    assert report["status"] == "ok"  # nosec B101


def test_signing_doctor_fails_unsigned_generated_pr(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = _repo(tmp_path / "example-aio")
    manifest = _manifest(tmp_path, repo_path)

    def fake_gh_json(command: list[str], *, check: bool):  # noqa: ARG001
        if command[0] == "api" and command[1].endswith("/protection"):
            return {"required_signatures": {"enabled": True}}
        if command[:2] == ["pr", "list"]:
            return [{"number": 12, "headRefName": "codex/update-example", "isCrossRepository": False}]
        if command[0] == "api" and command[1].endswith("/pulls/12/commits"):
            return [
                {
                    "commit": {
                        "verification": {
                            "verified": False,
                            "reason": "unsigned",
                        }
                    }
                }
            ]
        raise AssertionError(command)

    monkeypatch.setattr(signing, "_gh_json", fake_gh_json)

    report = signing_doctor_report(
        load_manifest(manifest), env=_app_env(), include_hooks=False
    )

    assert report["status"] == "failed"  # nosec B101
    assert "unsigned-generated-pr" in report["failure_classes"]  # nosec B101




def test_generated_pr_signature_blockers_ignores_cross_repo_prs(monkeypatch) -> None:
    def fake_gh_json(command: list[str], *, check: bool):  # noqa: ARG001
        if command[:2] == ["pr", "list"]:
            return [
                {
                    "number": 99,
                    "headRefName": "codex/block-release",
                    "isCrossRepository": True,
                }
            ]
        raise AssertionError(command)

    monkeypatch.setattr(signing, "_gh_json", fake_gh_json)

    assert signing.generated_pr_signature_blockers("wgross19/example-aio") == []  # nosec B101



def test_generated_pr_signature_blockers_fail_closed_on_gh_error(monkeypatch) -> None:
    monkeypatch.setattr(signing, "_gh_json", lambda command, *, check: None)  # noqa: ARG005

    assert signing.generated_pr_signature_blockers("wgross19/example-aio") == [  # nosec B101
        "unable to inspect generated PR signatures (gh authentication/api failure)"
    ]


def test_current_generated_pr_signature_blockers_fail_closed_on_gh_error(
    monkeypatch, tmp_path: Path
) -> None:
    repo_path = tmp_path / "example-aio"
    repo_path.mkdir()

    monkeypatch.setattr(
        signing,
        "_git",
        lambda command, *, cwd: subprocess.CompletedProcess(command, 0, "codex/update\n", ""),  # noqa: ARG005
    )
    monkeypatch.setattr(signing, "_gh_json", lambda command, *, check: None)  # noqa: ARG005

    assert signing.current_generated_pr_signature_blockers(
        "wgross19/example-aio", repo_path
    ) == [
        "unable to inspect generated PR signatures for branch codex/update (gh authentication/api failure)"
    ]
def test_workflow_writer_accepts_signed_github_app_create_pull_request(
    tmp_path: Path,
) -> None:
    repo_path = _repo(tmp_path / "awesome-unraid")
    workflow = repo_path / ".github" / "workflows" / "changelog.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("""
jobs:
  update:
    steps:
      - uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1
        with:
          client-id: ${{ vars.AIO_FLEET_APP_CLIENT_ID }}
          private-key: ${{ secrets.AIO_FLEET_APP_PRIVATE_KEY }}
      - uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1
        id: changelog-pr
        with:
          token: ${{ steps.fleetbot-token.outputs.token }}
          sign-commits: true
      - if: ${{ steps.changelog-pr.outputs.pull-request-number != '' }}
        run: test "${{ steps.changelog-pr.outputs.pull-request-commits-verified }}" = "true"
""")

    checks = workflow_writer_checks(
        SigningTarget(
            name="awesome-unraid",
            path=repo_path,
            github_repo="wgross19/awesome-unraid",
            role="catalog destination",
        )
    )

    assert checks == [  # nosec B101
        {
            "name": "automation-writers",
            "status": "ok",
            "class": "ok",
            "classification": "ok",
            "detail": "awesome-unraid:.github/workflows/changelog.yml: uses GitHub App signed PR commits",
            "repo": "awesome-unraid",
        }
    ]


def test_workflow_writer_requires_github_app_client_id(tmp_path: Path) -> None:
    repo_path = _repo(tmp_path / "awesome-unraid")
    workflow = repo_path / ".github" / "workflows" / "changelog.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("""
jobs:
  update:
    steps:
      - uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1
        with:
          app-id: ${{ secrets.AIO_FLEET_APP_ID }}
          private-key: ${{ secrets.AIO_FLEET_APP_PRIVATE_KEY }}
      - uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1
        id: changelog-pr
        with:
          token: ${{ steps.fleetbot-token.outputs.token }}
          sign-commits: true
      - if: ${{ steps.changelog-pr.outputs.pull-request-number != '' }}
        run: test "${{ steps.changelog-pr.outputs.pull-request-commits-verified }}" = "true"
""")

    checks = workflow_writer_checks(
        SigningTarget(
            name="awesome-unraid",
            path=repo_path,
            github_repo="wgross19/awesome-unraid",
            role="catalog destination",
        )
    )

    assert checks[0]["status"] == "failed"  # nosec B101
    assert checks[0]["class"] == "external-writer-gap"  # nosec B101


def test_workflow_writer_flags_pat_or_github_token_fallback(tmp_path: Path) -> None:
    repo_path = _repo(tmp_path / "awesome-unraid")
    workflow = repo_path / ".github" / "workflows" / "changelog.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("""
jobs:
  update:
    steps:
      - uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1
        with:
          token: ${{ secrets.AIO_FLEET_BOT_TOKEN || secrets.GITHUB_TOKEN }}
""")

    checks = workflow_writer_checks(
        SigningTarget(
            name="awesome-unraid",
            path=repo_path,
            github_repo="wgross19/awesome-unraid",
            role="catalog destination",
        )
    )

    assert checks[0]["status"] == "failed"  # nosec B101
    assert checks[0]["class"] == "external-writer-gap"  # nosec B101


def test_workflow_writer_flags_operation_gated_signature_check(tmp_path: Path) -> None:
    repo_path = _repo(tmp_path / "awesome-unraid")
    workflow = repo_path / ".github" / "workflows" / "changelog.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("""
jobs:
  update:
    steps:
      - uses: actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1
        with:
          client-id: ${{ vars.AIO_FLEET_APP_CLIENT_ID }}
          private-key: ${{ secrets.AIO_FLEET_APP_PRIVATE_KEY }}
      - uses: peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1
        id: changelog-pr
        with:
          token: ${{ steps.fleetbot-token.outputs.token }}
          sign-commits: true
      - if: ${{ steps.changelog-pr.outputs.pull-request-operation != 'none' }}
        run: test "${{ steps.changelog-pr.outputs.pull-request-commits-verified }}" = "true"
""")

    checks = workflow_writer_checks(
        SigningTarget(
            name="awesome-unraid",
            path=repo_path,
            github_repo="wgross19/awesome-unraid",
            role="catalog destination",
        )
    )

    assert checks[0]["status"] == "failed"  # nosec B101
    assert checks[0]["class"] == "external-writer-gap"  # nosec B101


def test_hook_doctor_flags_stray_local_trunk_overlay(tmp_path: Path) -> None:
    repo_path = _repo(tmp_path / "example-aio")
    hooks_dir = _git_dir(repo_path) / "aio-fleet-hooks"
    hooks_dir.mkdir()
    _git(repo_path, "config", "core.hooksPath", str(hooks_dir))
    (repo_path / ".trunk").mkdir()

    checks = signing._hook_checks(  # noqa: SLF001
        SigningTarget(
            name="example-aio",
            path=repo_path,
            github_repo="wgross19/example-aio",
            role="source",
        )
    )

    assert checks[0]["status"] == "ok"  # nosec B101
    assert checks[1]["status"] == "warning"  # nosec B101
    assert checks[1]["class"] == "external-writer-gap"  # nosec B101


def _ok_gh_json(command: list[str], *, check: bool):  # noqa: ARG001
    if command[0] == "api" and command[1].endswith("/protection"):
        return {"required_signatures": {"enabled": True}}
    if command[:2] == ["pr", "list"]:
        return []
    raise AssertionError(command)


def _manifest(
    tmp_path: Path, repo_path: Path, *, catalog_path: Path | None = None
) -> Path:
    dashboard = ""
    if catalog_path is not None:
        dashboard = f"""
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
      github_repo: wgross19/awesome-unraid
      public: true
"""
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
{dashboard}
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


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Tests")
    (path / "README.md").write_text("repo\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "chore(test): init repo")
    return path


def _app_env() -> dict[str, str]:
    return {
        "AIO_FLEET_APP_CLIENT_ID": "client-123",
        "AIO_FLEET_APP_ID": "123",
        "AIO_FLEET_APP_INSTALLATION_ID": "456",
        "AIO_FLEET_APP_PRIVATE_KEY": "private-key",
    }


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607
        ["git", *args],
        cwd=path,
        check=True,
        text=True,
        capture_output=True,
    )


def _git_dir(repo_path: Path) -> Path:
    git_dir = Path(
        subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--git-dir"], cwd=repo_path, text=True
        ).strip()
    )
    if not git_dir.is_absolute():
        git_dir = repo_path / git_dir
    return git_dir.resolve()
