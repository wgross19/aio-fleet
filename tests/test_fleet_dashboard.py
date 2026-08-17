from __future__ import annotations

import base64
import json
import subprocess  # nosec B404
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aio_fleet import fleet_dashboard
from aio_fleet.manifest import load_manifest
from aio_fleet.public_text import assert_public_text
from aio_fleet.upstream import UpstreamMonitorResult


class _FakeAssessment:
    def __init__(self, **values):
        self.values = values

    def to_dict(self):
        return {
            "safety_level": self.values.get("safety_level", "ok"),
            "confidence": self.values.get("confidence", 0.82),
            "config_delta": self.values.get("config_delta", "none"),
            "template_impact": self.values.get("template_impact", "no-xml-change"),
            "runtime_smoke": self.values.get("runtime_smoke", "not-configured"),
            "signals": self.values.get("signals", []),
            "warnings": self.values.get("warnings", []),
            "failures": self.values.get("failures", []),
            "next_action": self.values.get("next_action", "human review and merge"),
        }


@pytest.fixture(autouse=True)
def _stable_dashboard_dependencies(monkeypatch):
    monkeypatch.setattr(
        fleet_dashboard,
        "_github_actions_secret_exists",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "control_plane_health",
        lambda **_kwargs: {
            "state": "success",
            "workflow": "AIO Fleet Control Plane",
            "repo": "wgross19/aio-fleet",
            "controls_enabled": True,
            "latest": {"status": "completed", "conclusion": "success"},
            "last_success": {"status": "completed", "conclusion": "success"},
            "last_failure": {},
            "runs": [],
        },
    )

    def fake_release_plan(manifest, **kwargs):
        return [
            (
                {
                    "repo": repo.name,
                    "profile": "private-skipped",
                    "sha": "",
                    "latest_release_tag": "private-skipped",
                    "latest_github_release": {"state": "private-skipped"},
                    "next_version": "",
                    "next_action": "private-skipped",
                    "release_due": False,
                    "registry_failures": [],
                    "state": "private-skipped",
                }
                if kwargs.get("redact_private") and repo.raw.get("public") is not True
                else {
                    "repo": repo.name,
                    "state": "current",
                    "latest_release_tag": "",
                    "latest_github_release": {"state": "unknown"},
                    "next_version": "",
                    "next_action": "none",
                    "release_due": False,
                    "registry_failures": [],
                }
            )
            for repo in manifest.repos.values()
        ]

    monkeypatch.setattr(fleet_dashboard, "release_plan_for_manifest", fake_release_plan)


def test_alert_warnings_skip_local_missing_env_when_actions_secret_exists(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        fleet_dashboard,
        "_github_actions_secret_exists",
        lambda repo, name: repo == "wgross19/aio-fleet"
        and name == "AIO_FLEET_ALERT_WEBHOOK_URL",
    )

    assert (  # nosec B101
        fleet_dashboard.alert_warnings({}, issue_repo="wgross19/aio-fleet") == []
    )


def test_dashboard_summary_keeps_non_response_issues_out_of_posture() -> None:
    summary = fleet_dashboard.dashboard_summary(
        active_rows=[],
        activity_rows=[
            {
                "repo": "sure-aio",
                "open_prs": 0,
                "open_issues": 1,
                "needs_response_issues": 0,
                "clean_prs": 0,
                "blocked_prs": 0,
                "stale_prs": 0,
            }
        ],
        destination_rows=[],
        rehab_rows=[],
        registry_rows=[],
        release_rows=[],
        cleanup_rows=[],
        workflow={"state": "success"},
        warnings=[],
    )

    assert summary["open_issues"] == 1  # nosec B101
    assert summary["posture"] == "green"  # nosec B101
    assert summary["remote_posture"] == "green"  # nosec B101


def test_dashboard_renders_notify_only_update_and_webhook_warning(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
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
""")

    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="mem0-aio",
                component="aio",
                name="Mem0",
                strategy="notify",
                source="github-tags",
                current_version="v2.0.0",
                latest_version="v2.0.1",
                current_digest="",
                latest_digest="",
                version_update=True,
                digest_update=False,
                dockerfile=repo_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://github.com/mem0ai/mem0/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "assess_upstream_pr",
        lambda *_args, **_kwargs: _FakeAssessment(
            safety_level="manual",
            next_action="manual triage required before source PR",
        ),
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        env={},
    )

    body = str(report["body"])
    assert "manual triage; notify-only strategy" in body  # nosec B101
    assert "Safety Review" in body  # nosec B101
    assert (
        "uv run aio-fleet upstream assess --repo mem0-aio --format json" in body
    )  # nosec B101
    assert "AIO_FLEET_KUMA_PUSH_URL is not configured" not in body  # nosec B101
    assert "AIO_FLEET_ALERT_WEBHOOK_URL is not configured" in body  # nosec B101
    assert report["state"]["rows"][0]["strategy"] == "notify"  # nosec B101
    assert report["state"]["summary"]["triage_updates"] == 1  # nosec B101


def test_dashboard_renders_blocked_submodule_ref_without_private_leak(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
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
""")

    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="mem0-aio",
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
                dockerfile=repo_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://github.com/mem0ai/mem0/releases",
                submodule_path="openmemory",
                submodule_ref="codex/openmemory-v2.0.2-aio",
                blocked_reason="missing configured submodule ref",
                next_action="create and push codex/openmemory-v2.0.2-aio",
            )
        ],
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        env={},
    )

    row = report["state"]["rows"][0]
    assert row["check"] == "blocked"  # nosec B101
    assert row["safety"] == "blocked"  # nosec B101
    assert row["next_action"] == (  # nosec B101
        "create and push codex/openmemory-v2.0.2-aio"
    )
    assert report["state"]["summary"]["blocked_updates"] == 1  # nosec B101


def test_dashboard_marks_unsigned_pr_next_action(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
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
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_open_pr",
        lambda *_args, **_kwargs: {
            "number": 7,
            "url": "https://github.com/wgross19/example-aio/pull/7",
            "headRefOid": "a" * 40,
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {
                    "name": "aio-fleet / required",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
        },
    )
    monkeypatch.setattr(fleet_dashboard, "_signed_state", lambda *_args: "unsigned")
    monkeypatch.setattr(
        fleet_dashboard,
        "assess_upstream_pr",
        lambda *_args, **_kwargs: _FakeAssessment(),
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        env={
            "AIO_FLEET_KUMA_PUSH_URL": "https://kuma",
            "AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook",
        },
    )

    row = report["state"]["rows"][0]
    assert row["check"] == "success"  # nosec B101
    assert row["signed"] == "unsigned"  # nosec B101
    assert row["next_action"].startswith("regenerate/update PR")  # nosec B101


def test_issue_number_from_created_issue_url() -> None:
    assert (  # nosec B101
        fleet_dashboard._issue_number_from_url(
            "https://github.com/wgross19/aio-fleet/issues/55"
        )
        == 55
    )


def test_dashboard_renders_destination_and_rehab_groups(
    tmp_path: Path, monkeypatch
) -> None:
    active_path = tmp_path / "active"
    active_path.mkdir()
    catalog_path = tmp_path / "awesome-unraid"
    catalog_path.mkdir()
    rehab_path = tmp_path / "legacy-aio"
    rehab_path.mkdir()
    (rehab_path / "cliff.toml").write_text("[changelog]\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
      github_repo: wgross19/awesome-unraid
      public: true
      role: catalog destination
      catalog_path: {catalog_path}
  rehab_repos:
    legacy-aio:
      path: {rehab_path}
      github_repo: wgross19/legacy-aio
      public: true
      status: rehab
repos:
  example-aio:
    path: {active_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")

    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "repo_activity",
        lambda name, github_repo, _stale_days: {
            "repo": name,
            "github_repo": github_repo,
            "activity_state": "ok",
            "open_prs": 1 if name == "awesome-unraid" else 0,
            "open_issues": 2 if name == "legacy-aio" else 0,
            "draft_prs": 0,
            "blocked_prs": 0,
            "clean_prs": 1 if name == "awesome-unraid" else 0,
            "stale_prs": 0,
            "oldest_pr_age_days": 0,
            "oldest_issue_age_days": 0,
            "newest_issue_age_days": 0,
            "oldest_pr": {},
            "oldest_issue": {},
            "prs": [],
            "issues": [],
            "needs_response_issues": 0,
        },
    )
    monkeypatch.setattr(fleet_dashboard, "catalog_repo_failures", lambda *_args: [])
    monkeypatch.setattr(
        fleet_dashboard,
        "_git_state",
        lambda _path: {"path_exists": True, "branch": "main", "dirty": False},
    )

    report = fleet_dashboard.dashboard_report(load_manifest(manifest), env={})

    state = report["state"]
    body = str(report["body"])
    assert state["summary"]["destination_repos"] == 1  # nosec B101
    assert state["summary"]["rehab_repos"] == 1  # nosec B101
    assert set(load_manifest(manifest).repos) == {"example-aio"}  # nosec B101
    assert state["destination_repos"][0]["repo"] == "awesome-unraid"  # nosec B101
    assert state["rehab_repos"][0]["repo"] == "legacy-aio"  # nosec B101
    assert state["rehab_repos"][0]["cleanup_findings"] == 1  # nosec B101
    assert "Destination Repo" in body  # nosec B101
    assert "Rehab / Onboarding" in body  # nosec B101
    assert "- [ ] Rescan dashboard" in body  # nosec B101
    assert "- [ ] Run upstream monitor" in body  # nosec B101
    assert not any(row["repo"] == "legacy-aio" for row in state["rows"])  # nosec B101
    assert not any(
        row["repo"] == "awesome-unraid" for row in state["rows"]
    )  # nosec B101


def test_dashboard_treats_nanoclaw_as_active_repo(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "nanoclaw-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos: {{}}
repos:
  nanoclaw-aio:
    path: {repo_path}
    public: true
    app_slug: nanoclaw-aio
    image_name: wgross19/nanoclaw-aio
    docker_cache_scope: nanoclaw-aio-image
    pytest_image_tag: nanoclaw-aio:pytest
    publish_profile: multi-component
    components:
      aio:
        image_name: wgross19/nanoclaw-aio
      agent:
        image_name: wgross19/nanoclaw-agent
        dockerfile: components/nanoclaw-agent/Dockerfile
        release_policy: registry_only
""")

    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="nanoclaw-aio",
                component="aio",
                name="NanoClaw",
                strategy="pr",
                source="github-releases",
                current_version="v2.0.63",
                latest_version="v2.0.63",
                current_digest="",
                latest_digest="",
                version_update=False,
                digest_update=False,
                dockerfile=repo_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
            )
        ],
    )
    monkeypatch.setattr(fleet_dashboard, "cleanup_findings", lambda *_args: [])

    state = fleet_dashboard.dashboard_report(
        load_manifest(manifest), include_activity=False, env={}
    )["state"]

    assert state["summary"]["active_repos"] == 1  # nosec B101
    assert state["summary"]["rehab_repos"] == 0  # nosec B101
    assert state["rows"][0]["repo"] == "nanoclaw-aio"  # nosec B101
    assert state["rehab_repos"] == []  # nosec B101


def test_dashboard_skips_private_active_repo_activity(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "private-service-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  private-service-aio:
    path: {repo_path}
    github_repo: PrivateOrg/private-service-aio
    public: false
    app_slug: private-service-aio
    image_name: wgross19/private-service-aio
    docker_cache_scope: private-service-aio-image
    pytest_image_tag: private-service-aio:pytest
""")

    monkeypatch.setattr(fleet_dashboard, "monitor_repo", lambda *_args, **_kwargs: [])

    def unexpected_activity(*_args: object, **_kwargs: object):
        raise AssertionError("private repo activity should not be queried")

    monkeypatch.setattr(fleet_dashboard, "repo_activity", unexpected_activity)

    report = fleet_dashboard.dashboard_report(load_manifest(manifest), env={})

    activity = report["state"]["activity"][0]
    hidden = _hidden_dashboard_state(str(report["body"]))
    assert activity["activity_state"] == "private-skipped"  # nosec B101
    assert activity["github_repo"] == ""  # nosec B101
    assert activity["prs"] == []  # nosec B101
    assert "PrivateOrg/private-service-aio" not in hidden  # nosec B101
    assert "rotate production signing key" not in hidden  # nosec B101


def test_dashboard_redacts_private_registry_release_and_cleanup_state(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "private-service-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  private-service-aio:
    path: {repo_path}
    github_repo: PrivateOrg/private-service-aio
    public: false
    app_slug: private-service-aio
    image_name: wgross19/private-service-aio
    docker_cache_scope: private-service-aio-image
    pytest_image_tag: private-service-aio:pytest
""")

    def unexpected_private_collection(*_args: object, **_kwargs: object):
        raise AssertionError("private repo details should not be collected")

    monkeypatch.setattr(fleet_dashboard, "monitor_repo", unexpected_private_collection)
    monkeypatch.setattr(
        fleet_dashboard, "_repo_registry_states", unexpected_private_collection
    )
    monkeypatch.setattr(
        fleet_dashboard, "cleanup_findings", unexpected_private_collection
    )

    def malicious_release_plan(*_args: object, **_kwargs: object):
        return [
            {
                "repo": "private-service-aio",
                "profile": "upstream-aio-track",
                "sha": "d" * 40,
                "latest_release_tag": "99.0.0-private.1",
                "latest_changelog_version": "99.0.0-private.1",
                "latest_github_release": {
                    "state": "unknown",
                    "detail": "permission denied for PrivateOrg/private-service-aio",
                },
                "next_version": "99.0.0-private.2",
                "release_due": True,
                "catalog_sync_needed": True,
                "registry_state": "failed",
                "registry_tags": {
                    "dockerhub": ["wgross19/private-service-aio:secret"],
                    "ghcr": ["ghcr.io/wgross19/private-service-aio:secret"],
                },
                "registry_failures": [
                    "ghcr.io/wgross19/private-service-aio:secret: denied"
                ],
                "state": "publish-missing",
                "blockers": ["private blocker"],
                "warnings": ["private warning"],
                "next_action": (
                    "gh release view --repo PrivateOrg/private-service-aio"
                ),
            }
        ]

    monkeypatch.setattr(
        fleet_dashboard, "release_plan_for_manifest", malicious_release_plan
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_registry=True,
        env={"AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook"},
    )

    state = report["state"]
    hidden = _hidden_dashboard_state(str(report["body"]))
    row = state["rows"][0]
    release = state["releases"][0]
    assert row["registry"] == "private-skipped"  # nosec B101
    assert "registry_detail" not in row  # nosec B101
    assert "release_detail" not in row  # nosec B101
    assert state["registry"] == []  # nosec B101
    assert release["state"] == "private-skipped"  # nosec B101
    assert state["cleanup"][0]["state"] == "private-skipped"  # nosec B101
    assert state["actions"] == []  # nosec B101
    assert state["approvals"] == []  # nosec B101
    assert state["summary"]["publish_missing"] == 0  # nosec B101
    assert state["summary"]["release_due"] == 0  # nosec B101
    assert "PrivateOrg/private-service-aio" not in hidden  # nosec B101
    assert "wgross19/private-service-aio" not in hidden  # nosec B101
    assert "ghcr.io/wgross19/private-service-aio" not in hidden  # nosec B101
    assert "99.0.0-private" not in hidden  # nosec B101
    assert "dddddddddddd" not in hidden  # nosec B101
    assert "private blocker" not in hidden  # nosec B101


def test_dashboard_next_commands_include_component_registry_actions() -> None:
    row = {
        "repo": "sure-aio",
        "component": "sure-alpha",
        "current": "0.7.1-alpha.7",
        "latest": "0.7.1-alpha.7",
        "strategy": "pr",
        "update": False,
        "pr": "",
        "check": "not-needed",
        "signed": "not-needed",
        "registry": "failed:1",
        "registry_detail": {
            "repo": "sure-aio",
            "component": "sure-alpha",
            "sha": "a" * 40,
            "failures": ["wgross19/sure-aio-alpha:latest-alpha: missing"],
        },
        "release": "publish-missing",
        "release_detail": {
            "repo": "sure-aio",
            "component": "sure-alpha",
            "sha": "a" * 40,
            "state": "publish-missing",
            "operator_commands": {
                "registry_verify": "uv run aio-fleet registry verify --repo sure-aio --component sure-alpha --sha "
                + "a" * 40
                + " --verbose",
                "registry_publish": "uv run aio-fleet registry publish --repo sure-aio --component sure-alpha",
                "release_publish": "uv run aio-fleet release publish --repo sure-aio --component sure-alpha",
                "control_check_publish": "uv run aio-fleet control-check --repo sure-aio --sha "
                + "a" * 40
                + " --event push --publish --publish-component sure-alpha",
                "release_transaction": "uv run aio-fleet release transaction --repo sure-aio --component sure-alpha --sha "
                + "a" * 40
                + " --dry-run",
            },
        },
        "safety": "ok",
        "safety_confidence": "",
        "config_delta": "none",
        "template_impact": "no-xml-change",
        "runtime_smoke": "not-configured",
        "safety_signals": [],
        "safety_warnings": [],
        "safety_failures": [],
        "next_action": "none",
    }
    registry_rows = [row["registry_detail"]]
    release_rows = [row["release_detail"]]
    summary = fleet_dashboard.dashboard_summary(
        active_rows=[row],
        activity_rows=[],
        destination_rows=[],
        rehab_rows=[],
        registry_rows=registry_rows,
        release_rows=release_rows,
        cleanup_rows=[],
        workflow={"state": "success"},
        warnings=[],
    )
    body = fleet_dashboard.render_dashboard(
        {
            "generated_at": "2026-05-18T00:00:00+00:00",
            "issue_repo": "wgross19/aio-fleet",
            "warnings": [],
            "summary": summary,
            "rows": [row],
            "activity": [],
            "destination_repos": [],
            "rehab_repos": [],
            "registry": registry_rows,
            "releases": release_rows,
            "cleanup": [],
            "workflow": {"state": "success"},
        }
    )

    assert (  # nosec B101
        "uv run aio-fleet registry verify --repo sure-aio --component sure-alpha --sha "
        + "a" * 40
        + " --verbose"
        in body
    )
    assert (  # nosec B101
        "uv run aio-fleet release transaction --repo sure-aio --component sure-alpha --sha "
        + "a" * 40
        + " --dry-run"
        in body
    )
    assert (  # nosec B101
        "uv run aio-fleet registry publish --repo sure-aio --component sure-alpha"
        not in body
    )
    hidden = json.loads(_hidden_dashboard_state(body))
    assert hidden["releases"][0]["component"] == "sure-alpha"  # nosec B101


def test_dashboard_next_commands_include_transaction_for_release_due() -> None:
    row = {
        "repo": "sure-aio",
        "component": "sure-alpha",
        "update": False,
        "release_detail": {
            "repo": "sure-aio",
            "component": "sure-alpha",
            "sha": "b" * 40,
            "state": "release-due",
            "operator_commands": {
                "control_check_publish": "uv run aio-fleet control-check --repo sure-aio --sha "
                + "b" * 40
                + " --event push --publish --publish-component sure-alpha",
                "release_transaction": "uv run aio-fleet release transaction --repo sure-aio --component sure-alpha --sha "
                + "b" * 40
                + " --dry-run",
                "release_publish": "uv run aio-fleet release publish --repo sure-aio --component sure-alpha",
            },
        },
    }
    lines: list[str] = []

    fleet_dashboard._render_next_commands(lines, [row], [], [])
    body = "\n".join(lines)

    assert (  # nosec B101
        "uv run aio-fleet release transaction --repo sure-aio --component sure-alpha --sha "
        + "b" * 40
        + " --dry-run"
        in body
    )
    assert (  # nosec B101
        "uv run aio-fleet release publish --repo sure-aio --component sure-alpha"
        not in body
    )


def test_dashboard_next_commands_route_blocked_release_to_signing_doctor() -> None:
    row = {
        "repo": "sure-aio",
        "component": "aio",
        "update": False,
        "release_detail": {
            "repo": "sure-aio",
            "component": "aio",
            "sha": "b" * 40,
            "state": "blocked",
            "next_action": "uv run aio-fleet signing doctor --repo sure-aio --format json",
            "operator_commands": {
                "control_check_publish": "uv run aio-fleet control-check --repo sure-aio --sha "
                + "b" * 40
                + " --event push --publish --publish-component aio",
                "release_transaction": "uv run aio-fleet release transaction --repo sure-aio --component aio --sha "
                + "b" * 40
                + " --dry-run",
            },
        },
    }
    lines: list[str] = []

    fleet_dashboard._render_next_commands(lines, [row], [], [])
    body = "\n".join(lines)

    assert (  # nosec B101
        "uv run aio-fleet signing doctor --repo sure-aio --format json" in body
    )
    assert "control-check --repo sure-aio" not in body  # nosec B101


def test_dashboard_control_check_publish_requires_actionable_release() -> None:
    cases = [
        ("current", "c" * 40),
        ("catalog-sync-needed", "c" * 40),
        ("publish-missing", ""),
        ("release-due", "<sha>"),
    ]
    for state, sha in cases:
        row = {
            "repo": "sure-aio",
            "component": "sure-alpha",
            "update": False,
            "release_detail": {
                "repo": "sure-aio",
                "component": "sure-alpha",
                "sha": sha,
                "state": state,
                "operator_commands": {
                    "control_check_publish": "uv run aio-fleet control-check --repo sure-aio --sha "
                    + (sha or "<sha>")
                    + " --event push --publish --publish-component sure-alpha",
                    "registry_publish": "uv run aio-fleet registry publish --repo sure-aio --component sure-alpha",
                    "release_publish": "uv run aio-fleet release publish --repo sure-aio --component sure-alpha",
                },
            },
        }
        if state == "publish-missing":
            row["registry_detail"] = {
                "repo": "sure-aio",
                "component": "sure-alpha",
                "sha": "<sha>",
                "failures": ["wgross19/sure-aio-alpha:latest-alpha: missing"],
            }
        lines: list[str] = []

        fleet_dashboard._render_next_commands(lines, [row], [], [])
        body = "\n".join(lines)

        assert (
            "uv run aio-fleet control-check --repo sure-aio" not in body
        )  # nosec B101
        assert (
            "uv run aio-fleet registry publish --repo sure-aio --component sure-alpha"
            not in body
        )  # nosec B101


def test_catalog_sync_map_uses_source_catalog_asset_diff(tmp_path: Path) -> None:
    source_path = tmp_path / "sure-aio"
    catalog_path = tmp_path / "awesome-unraid"
    source_path.mkdir()
    catalog_path.mkdir()
    (source_path / "sure-aio.xml").write_text("<Container>new</Container>\n")
    (catalog_path / "sure-aio.xml").write_text("<Container>old</Container>\n")
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
      catalog_path: {catalog_path}
repos:
  sure-aio:
    path: {source_path}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
    catalog_assets:
      - source: sure-aio.xml
        target: sure-aio.xml
""")

    assert fleet_dashboard._catalog_sync_map(load_manifest(manifest)) == {  # nosec B101
        "sure-aio": True
    }

    (catalog_path / "sure-aio.xml").write_text("<Container>new</Container>\n")

    assert (
        fleet_dashboard._catalog_sync_map(load_manifest(manifest)) == {}
    )  # nosec B101


def test_dashboard_collects_public_active_repo_activity(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "example-aio"
    repo_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {repo_path}
    github_repo: wgross19/example-aio
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(fleet_dashboard, "monitor_repo", lambda *_args, **_kwargs: [])

    def fake_activity(name: str, github_repo: str, _stale_days: int):
        calls.append((name, github_repo))
        return {
            "repo": name,
            "github_repo": github_repo,
            "activity_state": "ok",
            "open_prs": 1,
            "open_issues": 0,
            "draft_prs": 0,
            "blocked_prs": 0,
            "clean_prs": 1,
            "stale_prs": 0,
            "oldest_pr_age_days": 1,
            "newest_issue_age_days": 0,
            "prs": [{"title": "public maintenance PR", "url": "https://example"}],
        }

    monkeypatch.setattr(fleet_dashboard, "repo_activity", fake_activity)

    report = fleet_dashboard.dashboard_report(load_manifest(manifest), env={})

    assert calls == [("example-aio", "wgross19/example-aio")]  # nosec B101
    assert "public maintenance PR" in _hidden_dashboard_state(
        str(report["body"])
    )  # nosec B101


def test_dashboard_skips_private_destination_and_rehab_activity(
    tmp_path: Path, monkeypatch
) -> None:
    catalog_path = tmp_path / "private-catalog"
    rehab_path = tmp_path / "private-rehab"
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    private-catalog:
      path: {catalog_path}
      github_repo: PrivateOrg/private-catalog
      catalog_path: {catalog_path}
  rehab_repos:
    private-rehab:
      path: {rehab_path}
      github_repo: PrivateOrg/private-rehab
      status: rehab
repos:
  private-service-aio:
    path: {tmp_path / "private-service-aio"}
    github_repo: PrivateOrg/private-service-aio
    public: false
    app_slug: private-service-aio
    image_name: wgross19/private-service-aio
    docker_cache_scope: private-service-aio-image
    pytest_image_tag: private-service-aio:pytest
""")

    def unexpected_activity(*_args: object, **_kwargs: object):
        raise AssertionError("private dashboard repo activity should not be queried")

    monkeypatch.setattr(fleet_dashboard, "repo_activity", unexpected_activity)
    monkeypatch.setattr(fleet_dashboard, "catalog_repo_failures", lambda *_args: [])
    monkeypatch.setattr(fleet_dashboard, "monitor_repo", lambda *_args, **_kwargs: [])

    report = fleet_dashboard.dashboard_report(load_manifest(manifest), env={})

    destination = report["state"]["destination_repos"][0]
    rehab = report["state"]["rehab_repos"][0]
    hidden = _hidden_dashboard_state(str(report["body"]))
    assert destination["activity_state"] == "private-skipped"  # nosec B101
    assert rehab["activity_state"] == "private-skipped"  # nosec B101
    assert destination["github_repo"] == ""  # nosec B101
    assert rehab["github_repo"] == ""  # nosec B101
    assert "PrivateOrg/private-catalog" not in hidden  # nosec B101
    assert "PrivateOrg/private-rehab" not in hidden  # nosec B101


def test_destination_row_tracks_ready_source_sync_queue(
    tmp_path: Path, monkeypatch
) -> None:
    app_path = tmp_path / "example-aio"
    app_path.mkdir()
    catalog_path = tmp_path / "awesome-unraid"
    catalog_path.mkdir()
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
dashboard:
  destination_repos:
    awesome-unraid:
      path: {catalog_path}
      github_repo: wgross19/awesome-unraid
      public: true
      role: catalog destination
      catalog_path: {catalog_path}
repos:
  example-aio:
    path: {app_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
""")
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
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
                dockerfile=app_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://example.invalid/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_open_pr",
        lambda *_args, **_kwargs: {
            "number": 12,
            "url": "https://github.com/wgross19/example-aio/pull/12",
            "headRefOid": "b" * 40,
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {
                    "name": "aio-fleet / required",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
        },
    )
    monkeypatch.setattr(fleet_dashboard, "_signed_state", lambda *_args: "verified")
    monkeypatch.setattr(
        fleet_dashboard,
        "assess_upstream_pr",
        lambda *_args, **_kwargs: _FakeAssessment(),
    )
    monkeypatch.setattr(fleet_dashboard, "catalog_repo_failures", lambda *_args: [])
    monkeypatch.setattr(
        fleet_dashboard,
        "repo_activity",
        lambda name, github_repo, _stale_days: {
            "repo": name,
            "github_repo": github_repo,
            "activity_state": "ok",
            "open_prs": 0,
            "open_issues": 0,
            "draft_prs": 0,
            "blocked_prs": 0,
            "clean_prs": 0,
            "stale_prs": 0,
            "oldest_pr_age_days": 0,
            "oldest_issue_age_days": 0,
            "newest_issue_age_days": 0,
            "oldest_pr": {},
            "oldest_issue": {},
            "prs": [],
            "issues": [],
            "needs_response_issues": 0,
        },
    )

    report = fleet_dashboard.dashboard_report(load_manifest(manifest), env={})

    destination = report["state"]["destination_repos"][0]
    assert destination["sync_queue_count"] == 1  # nosec B101
    assert destination["sync_queue"][0]["repo"] == "example-aio"  # nosec B101
    assert "| awesome-unraid | catalog destination | ok | 1 |" in str(
        report["body"]
    )  # nosec B101


def test_dashboard_registry_flag_renders_verified_tags(
    tmp_path: Path, monkeypatch
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
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="example-aio",
                component="aio",
                name="Example",
                strategy="pr",
                source="github-tags",
                current_version="1.0.0",
                latest_version="1.0.0",
                current_digest="",
                latest_digest="",
                version_update=False,
                digest_update=False,
                dockerfile=app_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://example.invalid/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_repo_registry_states",
        lambda _repo: {
            "aio": {
                "repo": "example-aio",
                "component": "aio",
                "sha": "a" * 40,
                "dockerhub": ["wgross19/example-aio:latest"],
                "ghcr": ["ghcr.io/wgross19/example-aio:latest"],
                "failures": [],
                "state": "ok",
                "verified_at": "2026-05-05T00:00:00+00:00",
            }
        },
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        include_registry=True,
        env={"AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook"},
    )

    row = report["state"]["rows"][0]
    assert row["registry"] == "ok:1+1 tags"  # nosec B101
    assert report["state"]["summary"]["registry_verified"] == 1  # nosec B101
    assert "Registry Verification" in str(report["body"])  # nosec B101


def test_dashboard_registry_mode_marks_publish_missing_release_queue(
    tmp_path: Path, monkeypatch
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
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="example-aio",
                component="aio",
                name="Example",
                strategy="pr",
                source="github-tags",
                current_version="1.0.0",
                latest_version="1.0.0",
                current_digest="",
                latest_digest="",
                version_update=False,
                digest_update=False,
                dockerfile=app_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://example.invalid/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_repo_registry_states",
        lambda _repo: {
            "aio": {
                "repo": "example-aio",
                "component": "aio",
                "sha": "a" * 40,
                "dockerhub": ["wgross19/example-aio:latest"],
                "ghcr": ["ghcr.io/wgross19/example-aio:latest"],
                "failures": ["wgross19/example-aio:latest: missing"],
                "state": "failed",
                "verified_at": "2026-05-05T00:00:00+00:00",
            }
        },
    )
    captured_kwargs: dict[str, object] = {}

    def fake_release_plan(manifest, **kwargs):
        captured_kwargs.update(kwargs)
        return [
            {
                "repo": "example-aio",
                "component": "aio",
                "state": "publish-missing",
                "profile": "upstream-aio-track",
                "sha": "a" * 40,
                "latest_release_tag": "1.0.0-aio.1",
                "latest_github_release": {"state": "ok", "tag": "1.0.0-aio.1"},
                "next_version": "1.0.0-aio.1",
                "release_due": False,
                "registry_verified": True,
                "registry_failures": ["wgross19/example-aio:latest: missing"],
                "registry_failure_evidence": [
                    {
                        "failure": "wgross19/example-aio:latest: missing",
                        "provenance": "remote-confirmed",
                    }
                ],
                "next_action": (
                    "uv run aio-fleet release transaction "
                    "--repo example-aio --sha " + "a" * 40 + " --dry-run"
                ),
                "operator_commands": {
                    "release_transaction": (
                        "uv run aio-fleet release transaction "
                        "--repo example-aio --sha " + "a" * 40 + " --dry-run"
                    )
                },
            }
        ]

    monkeypatch.setattr(fleet_dashboard, "release_plan_for_manifest", fake_release_plan)

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        include_registry=True,
        env={"AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook"},
    )

    row = report["state"]["rows"][0]
    assert captured_kwargs["include_registry"] is True  # nosec B101
    assert row["registry"] == "failed:1"  # nosec B101
    assert row["release"] == "publish-missing"  # nosec B101
    assert report["state"]["summary"]["publish_missing"] == 1  # nosec B101
    assert report["state"]["summary"]["posture"] == "blocked"  # nosec B101
    assert report["state"]["actions"][0]["kind"] == "registry-publish"  # nosec B101
    assert report["state"]["approvals"][0]["repo"] == "example-aio"  # nosec B101
    assert report["state"]["catalog"]["state"] == "ready"  # nosec B101
    assert report["state"]["standards"]["state"] == "ok"  # nosec B101
    assert "Fleet Command Center" in str(report["body"])  # nosec B101
    assert "Pending Approvals" in str(report["body"])  # nosec B101
    assert "publish-missing" in str(report["body"])  # nosec B101


def test_dashboard_registry_mode_splits_sha_tag_gaps(
    tmp_path: Path, monkeypatch
) -> None:
    app_path = tmp_path / "example-aio"
    app_path.mkdir()
    sha = "a" * 40
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
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
                repo="example-aio",
                component="aio",
                name="Example",
                strategy="pr",
                source="github-tags",
                current_version="1.0.0",
                latest_version="1.0.0",
                current_digest="",
                latest_digest="",
                version_update=False,
                digest_update=False,
                dockerfile=app_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://example.invalid/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_repo_registry_states",
        lambda _repo: {
            "aio": {
                "repo": "example-aio",
                "component": "aio",
                "sha": sha,
                "dockerhub": [f"wgross19/example-aio:sha-{sha}"],
                "ghcr": [f"ghcr.io/wgross19/example-aio:sha-{sha}"],
                "failures": [f"wgross19/example-aio:sha-{sha}: missing"],
                "state": "sha-tag-missing",
                "verified_at": "2026-05-05T00:00:00+00:00",
            }
        },
    )

    def fake_release_plan(_manifest, **_kwargs):
        return [
            {
                "repo": "example-aio",
                "component": "aio",
                "state": "sha-tag-missing",
                "profile": "upstream-aio-track",
                "sha": sha,
                "latest_release_tag": "1.0.0-aio.1",
                "latest_github_release": {"state": "ok", "tag": "1.0.0-aio.1"},
                "next_version": "1.0.0-aio.1",
                "release_due": False,
                "registry_verified": True,
                "registry_state": "sha-tag-missing",
                "registry_failures": [f"wgross19/example-aio:sha-{sha}: missing"],
                "registry_failure_evidence": [
                    {
                        "failure": f"wgross19/example-aio:sha-{sha}: missing",
                        "provenance": "remote-confirmed",
                    }
                ],
                "next_action": (
                    "python -m aio_fleet registry verify "
                    f"--repo example-aio --component aio --sha {sha} --verbose"
                ),
                "operator_commands": {},
            }
        ]

    monkeypatch.setattr(fleet_dashboard, "release_plan_for_manifest", fake_release_plan)

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        include_registry=True,
        env={"AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook"},
    )

    row = report["state"]["rows"][0]
    assert row["registry"] == "sha-missing:1"  # nosec B101
    assert row["release"] == "sha-tag-missing"  # nosec B101
    assert report["state"]["summary"]["registry_failures"] == 0  # nosec B101
    assert report["state"]["summary"]["sha_tag_missing"] == 1  # nosec B101
    assert report["state"]["summary"]["publish_missing"] == 0  # nosec B101
    assert report["state"]["summary"]["posture"] == "green"  # nosec B101
    assert report["state"]["actions"] == []  # nosec B101
    assert "SHA Tag Gaps" in str(report["body"])  # nosec B101


def test_dashboard_routes_safety_warning_to_triage(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        fleet_dashboard,
        "monitor_repo",
        lambda *_args, **_kwargs: [
            UpstreamMonitorResult(
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
                dockerfile=app_path / "Dockerfile",
                version_key="UPSTREAM_VERSION",
                digest_key="",
                release_notes_url="https://example.invalid/releases",
            )
        ],
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_open_pr",
        lambda *_args, **_kwargs: {
            "number": 12,
            "url": "https://github.com/wgross19/example-aio/pull/12",
            "headRefOid": "b" * 40,
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {
                    "name": "aio-fleet / required",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
        },
    )
    monkeypatch.setattr(fleet_dashboard, "_signed_state", lambda *_args: "verified")
    monkeypatch.setattr(
        fleet_dashboard,
        "assess_upstream_pr",
        lambda *_args, **_kwargs: _FakeAssessment(
            safety_level="warn",
            config_delta="example-aio.xml: +1 -0",
            template_impact="review-template-config-delta",
            runtime_smoke="not-configured",
            warnings=["release notes mention review keyword(s): config"],
            next_action="release notes mention review keyword(s): config",
        ),
    )

    report = fleet_dashboard.dashboard_report(
        load_manifest(manifest),
        include_activity=False,
        env={
            "AIO_FLEET_KUMA_PUSH_URL": "https://kuma",
            "AIO_FLEET_ALERT_WEBHOOK_URL": "https://hook",
        },
    )

    body = str(report["body"])
    row = report["state"]["rows"][0]
    assert row["safety"] == "warn"  # nosec B101
    assert row["config_delta"] == "example-aio.xml: +1 -0"  # nosec B101
    state = report["state"]
    assert state["summary"]["triage_updates"] == 1  # nosec B101
    assert "Needs Triage" in body  # nosec B101
    assert "Safety Review" in body  # nosec B101
    assert "| example-aio | aio | 1.0.0 | 1.1.0 |" in body  # nosec B101
    hidden = _hidden_dashboard_state(body)
    assert '"safety": "warn"' in hidden  # nosec B101


def test_dashboard_state_comment_is_safe_for_pr_titles() -> None:
    state = {
        "generated_at": "2026-05-05T00:00:00+00:00",
        "summary": {},
        "warnings": [],
        "rows": [],
        "activity": [
            {
                "repo": "example-aio",
                "prs": [
                    {
                        "title": "--><a href='https://evil.example'>click</a><!--",
                    }
                ],
            }
        ],
        "destination_repos": [],
        "rehab_repos": [],
    }

    body = fleet_dashboard.render_dashboard(state)
    hidden_block = body.split(fleet_dashboard.STATE_START_BASE64, 1)[1].split(
        fleet_dashboard.STATE_END, 1
    )[0]

    assert "-->" not in hidden_block  # nosec B101
    assert "<a href='https://evil.example'>" not in body  # nosec B101
    assert "evil.example" in _hidden_dashboard_state(body)  # nosec B101


def test_dashboard_redacts_non_public_text_from_body_and_hidden_state() -> None:
    unsafe_path = "/Users/shadowbook/Documents/aio-fleet/.venv/bin/python"
    unsafe_worktree = ".codex/worktrees/2551/aio-fleet"
    unsafe_webhook = "https://discord.com/api/webhooks/123/secret"
    state = {
        "schema_version": 4,
        "generated_at": "2026-05-05T00:00:00+00:00",
        "issue_repo": "wgross19/aio-fleet",
        "summary": {"posture": "blocked"},
        "warnings": [f"debug command used {unsafe_path}"],
        "rows": [
            {
                "repo": "example-aio",
                "component": "aio",
                "current": "1.0.0",
                "latest": "1.1.0",
                "strategy": "pr",
                "update": True,
                "pr": "",
                "check": "missing",
                "signed": "missing",
                "registry": "failed:1",
                "release": "blocked",
                "safety": "blocked",
                "config_delta": "unknown",
                "template_impact": "unknown",
                "runtime_smoke": "unknown",
                "safety_failures": [unsafe_webhook],
                "next_action": f"rerun {unsafe_path}",
            }
        ],
        "activity": [],
        "destination_repos": [],
        "rehab_repos": [],
        "registry": [
            {
                "repo": "example-aio",
                "component": "aio",
                "sha": "a" * 40,
                "dockerhub": [],
                "ghcr": [],
                "failures": [f"artifact came from {unsafe_worktree}"],
                "state": "failed",
                "verified_at": "2026-05-05T00:00:00+00:00",
            }
        ],
        "releases": [],
        "cleanup": [
            {
                "repo": "example-aio",
                "findings_count": 1,
                "findings": [{"path": unsafe_path, "reason": unsafe_webhook}],
            }
        ],
        "workflow": {},
    }

    body = fleet_dashboard.render_dashboard(state)
    hidden = _hidden_dashboard_state(body)

    for unsafe in (unsafe_path, unsafe_worktree, unsafe_webhook):
        assert unsafe not in body  # nosec B101
        assert unsafe not in hidden  # nosec B101
    assert_public_text(body, context="dashboard body")
    assert_public_text(hidden, context="dashboard hidden state")
    assert "<redacted: macOS home path>" in body  # nosec B101
    assert "<redacted: Discord webhook URL>" in hidden  # nosec B101


def test_dashboard_body_compacts_before_github_issue_limit() -> None:
    rows = []
    for index in range(80):
        rows.append(
            {
                "repo": f"example-{index}-aio",
                "component": "aio",
                "current": "1.0.0",
                "latest": "1.1.0",
                "strategy": "pr",
                "update": True,
                "pr": "",
                "check": "missing",
                "signed": "missing",
                "registry": "not-run",
                "release": "after-merge",
                "safety": "warn",
                "safety_confidence": "",
                "config_delta": "unknown",
                "template_impact": "review-template-config-delta",
                "runtime_smoke": "unknown",
                "safety_signals": [],
                "safety_warnings": ["manual review required"],
                "safety_failures": [],
                "next_action": "review " + ("x" * 2000),
            }
        )
    state = {
        "schema_version": 4,
        "generated_at": "2026-05-05T00:00:00+00:00",
        "issue_repo": "wgross19/aio-fleet",
        "summary": {
            "posture": "action required",
            "remote_posture": "action required",
            "local_posture": "clean",
            "active_repos": 80,
            "upstream_updates": 80,
            "triage_updates": 80,
        },
        "warnings": [],
        "rows": rows,
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

    body = fleet_dashboard.render_dashboard(state)
    hidden = json.loads(_hidden_dashboard_state(body))

    assert len(body) <= fleet_dashboard.GITHUB_ISSUE_BODY_SOFT_LIMIT  # nosec B101
    assert "Detailed fleet tables were compacted" in body  # nosec B101
    assert "## Controls" in body  # nosec B101
    assert hidden["summary"]["active_repos"] == 80  # nosec B101
    assert "x" * 1000 not in hidden  # nosec B101


def test_dashboard_body_emergency_compacts_oversized_hidden_state() -> None:
    state = {
        "schema_version": 4,
        "generated_at": "2026-05-05T00:00:00+00:00",
        "issue_repo": "wgross19/aio-fleet",
        "summary": {
            "posture": "action required",
            "remote_posture": "action required",
            "local_posture": "clean",
            "active_repos": 10,
            "upstream_updates": 3,
            "triage_updates": 1,
            "registry_failures": 2,
        },
        "warnings": [],
        "rows": [],
        "actions": [],
        "failures": [],
        "approvals": [],
        "catalog": {},
        "standards": {},
        "candidates": {f"candidate-{index}": "x" * 2000 for index in range(200)},
        "activity": [],
        "destination_repos": [],
        "rehab_repos": [],
        "registry": [],
        "releases": [],
        "cleanup": [],
        "workflow": {f"run-{index}": "y" * 2000 for index in range(200)},
    }

    body = fleet_dashboard.render_dashboard(state)
    hidden = json.loads(_hidden_dashboard_state(body))

    assert len(body) <= fleet_dashboard.GITHUB_ISSUE_BODY_SOFT_LIMIT  # nosec B101
    assert "Dashboard detail was compacted" in body  # nosec B101
    assert "## Controls" in body  # nosec B101
    assert hidden["summary"]["active_repos"] == 10  # nosec B101
    assert hidden["workflow"] == {}  # nosec B101
    assert hidden["candidates"] == {}  # nosec B101
    assert "x" * 1000 not in body  # nosec B101
    assert "y" * 1000 not in body  # nosec B101


def test_repo_activity_classifies_open_prs_and_issues(monkeypatch) -> None:
    def days_ago(days: int) -> str:
        value = datetime.now(UTC).replace(microsecond=0) - timedelta(days=days)
        return value.isoformat().replace("+00:00", "Z")

    def fake_gh_json(args: list[str]):
        if args[:2] == ["pr", "list"]:
            return [
                {
                    "number": 1,
                    "title": "ready",
                    "url": "https://github.com/wgross19/example/pull/1",
                    "isDraft": False,
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                    "createdAt": days_ago(9),
                },
                {
                    "number": 2,
                    "title": "draft",
                    "url": "https://github.com/wgross19/example/pull/2",
                    "isDraft": True,
                    "mergeStateStatus": "CLEAN",
                    "statusCheckRollup": [],
                    "createdAt": days_ago(2),
                },
                {
                    "number": 3,
                    "title": "blocked",
                    "url": "https://github.com/wgross19/example/pull/3",
                    "isDraft": False,
                    "mergeStateStatus": "DIRTY",
                    "statusCheckRollup": [],
                    "createdAt": days_ago(2),
                },
            ]
        if args[:2] == ["issue", "list"]:
            return [
                {
                    "number": 9,
                    "title": "one",
                    "url": "https://github.com/wgross19/example/issues/9",
                    "createdAt": days_ago(3),
                    "labels": [{"name": "needs-response"}],
                },
                {
                    "number": 10,
                    "title": "two",
                    "url": "https://github.com/wgross19/example/issues/10",
                    "createdAt": days_ago(12),
                    "labels": [],
                },
            ]
        raise AssertionError(args)

    monkeypatch.setattr(fleet_dashboard, "_gh_json", fake_gh_json)

    activity = fleet_dashboard.repo_activity(
        "example-aio", "wgross19/example-aio", stale_days=7
    )

    assert activity["open_prs"] == 3  # nosec B101
    assert activity["clean_prs"] == 1  # nosec B101
    assert activity["draft_prs"] == 1  # nosec B101
    assert activity["blocked_prs"] == 1  # nosec B101
    assert activity["stale_prs"] == 1  # nosec B101
    assert activity["open_issues"] == 2  # nosec B101
    assert activity["needs_response_issues"] == 1  # nosec B101
    assert activity["oldest_issue"]["number"] == 10  # nosec B101
    assert activity["issues"][0]["number"] == 9  # nosec B101


def test_repo_activity_failure_is_non_blocking(monkeypatch) -> None:
    def fake_gh_json(_args: list[str]):
        raise RuntimeError("api down")

    monkeypatch.setattr(fleet_dashboard, "_gh_json", fake_gh_json)

    activity = fleet_dashboard.repo_activity(
        "example-aio", "wgross19/example-aio", stale_days=7
    )

    assert activity["activity_state"] == "unknown"  # nosec B101
    assert activity["open_prs"] == "unknown"  # nosec B101


def test_dashboard_command_parser_detects_checked_controls() -> None:
    commands = fleet_dashboard.dashboard_commands_from_body(
        "\n".join(
            [
                "## Controls",
                "",
                "- [x] Rescan dashboard",
                "- [ ] Run upstream monitor",
            ]
        )
    )

    assert commands == {  # nosec B101
        "rescan": True,
        "upstream_monitor": False,
        "standards_reconcile": False,
        "queue_publish_checks": False,
    }


def test_find_dashboard_issue_prefers_labeled_canonical_issue(monkeypatch) -> None:
    responses = {
        (
            "issue",
            "list",
            "--repo",
            "wgross19/aio-fleet",
            "--state",
            "open",
            "--label",
        ): [
            {
                "number": 55,
                "title": "Fleet Command Center",
                "url": "https://github.com/wgross19/aio-fleet/issues/55",
                "updatedAt": "2026-05-04T19:00:00Z",
                "body": "<!-- aio-fleet-dashboard-state",
                "labels": [{"name": "fleet-dashboard"}],
            }
        ],
        (
            "issue",
            "list",
            "--repo",
            "wgross19/aio-fleet",
            "--state",
            "open",
            "--search",
        ): [
            {
                "number": 58,
                "title": "Fleet Command Center",
                "url": "https://github.com/wgross19/aio-fleet/issues/58",
                "updatedAt": "2026-05-04T12:00:00Z",
                "body": "<!-- aio-fleet-dashboard-state",
                "labels": [],
            },
            {
                "number": 55,
                "title": "Fleet Command Center",
                "url": "https://github.com/wgross19/aio-fleet/issues/55",
                "updatedAt": "2026-05-04T19:00:00Z",
                "body": "<!-- aio-fleet-dashboard-state",
                "labels": [{"name": "fleet-dashboard"}],
            },
        ],
    }

    def fake_run(command: list[str], *, check=True, cwd=None, cli_scope="activity"):
        del check, cwd, cli_scope
        key = tuple(command[1:8])
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(responses[key]),
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    issue = fleet_dashboard._find_dashboard_issue(
        "wgross19/aio-fleet", label="fleet-dashboard"
    )

    assert issue is not None  # nosec B101
    assert issue["number"] == 55  # nosec B101


def test_dashboard_issue_by_number_uses_direct_view(monkeypatch) -> None:
    def fake_run(command: list[str], *, check=True, cwd=None, cli_scope="activity"):
        del check, cwd, cli_scope
        assert command[:4] == ["gh", "issue", "view", "55"]  # nosec B101
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "number": 55,
                    "title": "Fleet Command Center",
                    "url": "https://github.com/wgross19/aio-fleet/issues/55",
                    "updatedAt": "2026-05-04T19:00:00Z",
                    "body": "<!-- aio-fleet-dashboard-state",
                    "labels": [{"name": "fleet-dashboard"}],
                    "state": "OPEN",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    issue = fleet_dashboard._dashboard_issue_by_number("wgross19/aio-fleet", 55)

    assert issue is not None  # nosec B101
    assert issue["number"] == 55  # nosec B101


def test_upsert_dashboard_issue_updates_body_from_stdin(monkeypatch) -> None:
    body = "# Dashboard\n" + ("row\n" * 5000)
    calls: list[tuple[list[str], str | None]] = []

    monkeypatch.setattr(
        fleet_dashboard,
        "_dashboard_issue_by_number",
        lambda _repo, _number: {
            "number": 55,
            "url": "https://github.com/wgross19/aio-fleet/issues/55",
        },
    )
    monkeypatch.setattr(
        fleet_dashboard, "_ensure_label", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_add_dashboard_label",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        fleet_dashboard,
        "_close_duplicate_dashboard_issues",
        lambda *_args, **_kwargs: None,
    )

    def fake_run(
        command: list[str],
        *,
        check=True,
        cwd=None,
        cli_scope="activity",
        input_text=None,
    ):
        del check, cwd, cli_scope
        calls.append((command, input_text))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    result = fleet_dashboard.upsert_dashboard_issue(
        issue_repo="wgross19/aio-fleet",
        issue_number=55,
        body=body,
        dry_run=False,
    )

    assert result.action == "updated"  # nosec B101
    command, input_text = calls[0]
    assert "--body-file" in command  # nosec B101
    assert command[command.index("--body-file") + 1] == "-"  # nosec B101
    assert "--body" not in command  # nosec B101
    assert body not in command  # nosec B101
    assert input_text == body  # nosec B101


def test_upsert_dashboard_issue_creates_body_from_stdin(monkeypatch) -> None:
    body = "# Dashboard\n" + ("row\n" * 5000)
    calls: list[tuple[list[str], str | None]] = []

    monkeypatch.setattr(
        fleet_dashboard, "_find_dashboard_issue", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        fleet_dashboard, "_ensure_label", lambda *_args, **_kwargs: None
    )

    def fake_run(
        command: list[str],
        *,
        check=True,
        cwd=None,
        cli_scope="activity",
        input_text=None,
    ):
        del check, cwd, cli_scope
        calls.append((command, input_text))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="https://github.com/wgross19/aio-fleet/issues/58\n",
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    result = fleet_dashboard.upsert_dashboard_issue(
        issue_repo="wgross19/aio-fleet",
        body=body,
        dry_run=False,
    )

    assert result.action == "created"  # nosec B101
    assert result.number == 58  # nosec B101
    command, input_text = calls[0]
    assert "--body-file" in command  # nosec B101
    assert command[command.index("--body-file") + 1] == "-"  # nosec B101
    assert "--body" not in command  # nosec B101
    assert body not in command  # nosec B101
    assert input_text == body  # nosec B101


def test_dashboard_issue_commands_accepts_labeled_dashboard_issue(
    monkeypatch,
) -> None:
    def fake_run(command: list[str], *, check=True, cwd=None, cli_scope="activity"):
        del check, cwd, cli_scope
        assert command[:4] == ["gh", "issue", "view", "55"]  # nosec B101
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "number": 55,
                    "title": "Fleet Command Center",
                    "state": "OPEN",
                    "body": (
                        "- [x] Run upstream monitor\n"
                        "<!-- aio-fleet-dashboard-state\n{}"
                    ),
                    "labels": [{"name": "fleet-dashboard"}],
                    "url": "https://github.com/wgross19/aio-fleet/issues/55",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    result = fleet_dashboard.dashboard_issue_commands(
        issue_repo="wgross19/aio-fleet", issue_number=55
    )

    assert result["is_dashboard"] is True  # nosec B101
    assert result["requested"] is True  # nosec B101
    assert result["commands"]["upstream_monitor"] is True  # nosec B101


def test_dashboard_issue_commands_rejects_unlabeled_body_controls(
    monkeypatch,
) -> None:
    def fake_run(command: list[str], *, check=True, cwd=None, cli_scope="activity"):
        del check, cwd, cli_scope
        assert command[:4] == ["gh", "issue", "view", "55"]  # nosec B101
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "number": 55,
                    "title": "Fleet Command Center",
                    "state": "OPEN",
                    "body": (
                        "- [x] Run upstream monitor\n"
                        "<!-- aio-fleet-dashboard-state\n{}"
                    ),
                    "labels": [],
                    "url": "https://github.com/wgross19/aio-fleet/issues/55",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    result = fleet_dashboard.dashboard_issue_commands(
        issue_repo="wgross19/aio-fleet", issue_number=55
    )

    assert result["is_dashboard"] is False  # nosec B101
    assert result["requested"] is False  # nosec B101
    assert result["commands"] == {}  # nosec B101


def test_dashboard_gh_reads_prefer_app_token(monkeypatch) -> None:
    captured_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object):
        nonlocal captured_env
        captured_env = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps([]),
            stderr="",
        )

    monkeypatch.setenv("AIO_FLEET_DASHBOARD_TOKEN", "app-token")
    monkeypatch.setenv("AIO_FLEET_ISSUE_TOKEN", "issue-token")
    monkeypatch.setenv("GH_TOKEN", "lower-priority-token")
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fleet_dashboard._gh_json(["pr", "list", "--repo", "wgross19/private"])

    assert result == []  # nosec B101
    assert captured_env["GH_TOKEN"] == "app-token"  # nosec B101
    assert "AIO_FLEET_ISSUE_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101


def test_dashboard_issue_reads_prefer_issue_token(monkeypatch) -> None:
    captured_env: dict[str, str] = {}

    def fake_run(*args: object, **kwargs: object):
        nonlocal captured_env
        captured_env = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"number": 55}),
            stderr="",
        )

    monkeypatch.setenv("AIO_FLEET_DASHBOARD_TOKEN", "app-token")
    monkeypatch.setenv("AIO_FLEET_ISSUE_TOKEN", "issue-token")
    monkeypatch.setenv("GH_TOKEN", "lower-priority-token")
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fleet_dashboard._gh_json(["issue", "view", "55"], cli_scope="issue")

    assert result == {"number": 55}  # nosec B101
    assert captured_env["GH_TOKEN"] == "issue-token"  # nosec B101
    assert "AIO_FLEET_DASHBOARD_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101


def _hidden_dashboard_state(body: str) -> str:
    hidden = body.split(fleet_dashboard.STATE_START_BASE64, 1)[1].split(
        fleet_dashboard.STATE_END, 1
    )[0]
    return base64.b64decode(hidden.strip()).decode("utf-8")


def test_dashboard_issue_commands_rejects_unlabeled_non_dashboard_issue(
    monkeypatch,
) -> None:
    def fake_run(command: list[str], *, check=True, cwd=None, cli_scope="activity"):
        del check, cwd, cli_scope
        assert command[:4] == ["gh", "issue", "view", "55"]  # nosec B101
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "number": 55,
                    "title": "Fleet Command Center",
                    "state": "OPEN",
                    "body": "ordinary issue body",
                    "labels": [],
                    "url": "https://github.com/wgross19/aio-fleet/issues/55",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(fleet_dashboard, "_run", fake_run)

    result = fleet_dashboard.dashboard_issue_commands(
        issue_repo="wgross19/aio-fleet", issue_number=55
    )

    assert result["is_dashboard"] is False  # nosec B101
    assert result["requested"] is False  # nosec B101
    assert result["commands"] == {}  # nosec B101
