from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from aio_fleet import workflow_jobs
from aio_fleet.change_scope import CHECK_MODE_FULL
from aio_fleet.poll import PublishPathResolutionError
from aio_fleet.workflow_jobs import (
    poll_outputs,
    registry_audit_checkouts,
    render_registry_summary,
    render_upstream_summary,
    upstream_poll_targets,
)


def _write_upstream_workflow_manifest(tmp_path: Path, checkout: Path) -> Path:
    manifest = tmp_path / "fleet.yml"
    manifest.write_text(f"""
owner: wgross19
repos:
  sure-aio:
    path: {checkout}
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio
    pytest_image_tag: sure-aio:pytest
    upstream_monitor:
      - component: aio
        name: Sure
        source: github-releases
        repo: sureapp/sure
        dockerfile: Dockerfile
        version_key: UPSTREAM_VERSION
        strategy: pr
        release_notes_url: https://example.test/releases
""")
    return manifest


def _upstream_update_payload(checkout: Path) -> dict[str, object]:
    return {
        "repo": "sure-aio",
        "component": "aio",
        "name": "Sure",
        "strategy": "pr",
        "source": "github-releases",
        "current_version": "1.0.0",
        "latest_version": "1.0.1",
        "current_digest": "",
        "latest_digest": "",
        "version_update": True,
        "digest_update": False,
        "updates_available": True,
        "dockerfile": str(checkout / "Dockerfile"),
        "release_notes_url": "https://example.test/releases",
        "state": "updates",
    }


def _clear_secret_env(monkeypatch) -> None:
    for key in list(os.environ):
        if workflow_jobs._secret_environment_key(key):
            monkeypatch.delenv(key, raising=False)


def test_poll_outputs_writes_github_matrix(tmp_path: Path) -> None:
    report = tmp_path / "poll-targets.json"
    output = tmp_path / "github-output.txt"
    report.write_text(json.dumps({"targets": [{"repo": "sure-aio"}]}))

    payload = poll_outputs(
        report_path=report,
        run_checks=True,
        github_output=output,
    )

    assert payload["has_targets"] is True  # nosec B101
    text = output.read_text()
    assert "run_checks=true" in text  # nosec B101
    assert "targets<<__AIO_FLEET_TARGETS__" in text  # nosec B101


def test_upstream_poll_targets_emits_publish_disabled_pr_targets(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report = tmp_path / "upstream-report.json"
    head_sha = "a" * 40
    report.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "action": "upserted-pr",
                        "branch": "codex/upstream-sure-aio-1.0.1",
                        "url": "https://github.com/wgross19/sure-aio/pull/1",
                        "sha": head_sha,
                    }
                ]
            }
        )
    )
    output = tmp_path / "github-output.txt"

    payload = upstream_poll_targets(
        report_path=report,
        manifest_path=manifest,
        github_output=output,
    )

    assert payload["run_checks"] is True  # nosec B101
    assert payload["has_targets"] is True  # nosec B101
    assert len(payload["targets"]) == 1  # nosec B101
    target = payload["targets"][0]
    assert target["repo"] == "sure-aio"  # nosec B101
    assert target["sha"] == head_sha  # nosec B101
    assert target["event"] == "pull_request"  # nosec B101
    assert target["publish"] is False  # nosec B101
    assert target["check_mode"] == CHECK_MODE_FULL  # nosec B101
    assert target["source"] == "upstream-pr:codex/upstream-sure-aio-1.0.1"  # nosec B101
    text = output.read_text()
    assert "run_checks=true" in text  # nosec B101
    assert "targets<<__AIO_FLEET_TARGETS__" in text  # nosec B101


def test_upstream_poll_targets_skips_when_no_pr_opened(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    checkout.mkdir()
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report = tmp_path / "upstream-report.json"
    report.write_text(
        json.dumps(
            {
                "repos": [
                    {"repo": "sure-aio", "action": "skipped", "reason": "no-updates"}
                ]
            }
        )
    )
    output = tmp_path / "github-output.txt"

    payload = upstream_poll_targets(
        report_path=report,
        manifest_path=manifest,
        github_output=output,
    )

    assert payload["run_checks"] is False  # nosec B101
    assert payload["has_targets"] is False  # nosec B101
    assert payload["targets"] == []  # nosec B101
    assert "run_checks=false" in output.read_text()  # nosec B101


def test_upstream_monitor_subprocess_env_uses_disposable_home(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/real-home")
    env = workflow_jobs._upstream_monitor_subprocess_env()
    assert env["HOME"] != "/tmp/real-home"  # nosec B101
    assert "aio-fleet-upstream-monitor-home-" in env["HOME"]  # nosec B101


def test_upstream_summary_renders_updates(tmp_path: Path) -> None:
    report = tmp_path / "upstream-report.json"
    report.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "results": [
                            {
                                "component": "aio",
                                "current_version": "0.7.0",
                                "latest_version": "0.7.1",
                                "updates_available": True,
                            }
                        ],
                    }
                ]
            }
        )
    )

    text = render_upstream_summary(report_path=report, output_path=None)

    assert "`sure-aio`: updates available" in text  # nosec B101
    assert "0.7.0 -> 0.7.1" in text  # nosec B101


def test_changed_paths_uses_secretless_git_invocation(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=" M Dockerfile\n",
            stderr="",
        )

    monkeypatch.setenv("AIO_FLEET_WORKFLOW_TOKEN", "workflow-token")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check-token")
    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)

    changed = workflow_jobs._changed_paths(repo_path)

    assert changed == {"Dockerfile"}  # nosec B101
    assert captured["args"] == [  # nosec B101
        "git",
        "-c",
        "core.fsmonitor=",
        "status",
        "--porcelain",
        "--untracked-files=all",
    ]
    env = captured["env"]
    assert isinstance(env, dict)  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in env  # nosec B101
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull  # nosec B101
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"  # nosec B101
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # nosec B101


def test_upstream_summary_renders_blocked_submodule_ref(tmp_path: Path) -> None:
    report = tmp_path / "upstream-report.json"
    report.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "mem0-aio",
                        "results": [
                            {
                                "component": "openmemory",
                                "current_version": "v2.0.1",
                                "latest_version": "v2.0.2",
                                "updates_available": True,
                                "state": "blocked",
                                "blocked": True,
                                "blocked_reason": "missing configured submodule ref",
                                "next_action": (
                                    "create and push codex/openmemory-v2.0.2-aio"
                                ),
                            }
                        ],
                    }
                ]
            }
        )
    )

    text = render_upstream_summary(report_path=report, output_path=None)

    assert "`mem0-aio`: blocked" in text  # nosec B101
    assert "missing configured submodule ref" in text  # nosec B101


def test_registry_summary_renders_missing_tags(tmp_path: Path) -> None:
    report = tmp_path / "registry-report.json"
    report.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "dify-aio",
                        "component": "aio",
                        "sha": "a" * 40,
                        "dockerhub": ["wgross19/dify-aio:latest"],
                        "ghcr": [],
                        "failures": ["missing tag"],
                    }
                ]
            }
        )
    )

    text = render_registry_summary(report_path=report, status="1", output_path=None)

    assert "Registry Audit" in text  # nosec B101
    assert "dify-aio: missing tag" in text  # nosec B101


def test_registry_audit_skips_components_not_required_for_current_head(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "fleet.yml"
    checkout_root = tmp_path / "checkouts"
    output = tmp_path / "registry-report.json"
    manifest.write_text("""
owner: wgross19
repos:
  sure-aio:
    path: /tmp/sure-aio
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio
    pytest_image_tag: sure-aio:pytest
    publish_profile: multi-component
    components:
      aio: {}
      sure-alpha:
        image_name: wgross19/sure-aio-alpha
        dockerfile: Dockerfile.alpha
""")

    def fake_checkout_refs(refs, *, token: str, submodules: str):
        results = []
        for name, github_repo, path in refs:
            path.mkdir(parents=True)
            results.append(
                {"repo": name, "github_repo": github_repo, "path": str(path)}
            )
        return results

    def fake_check_output(*_args, **_kwargs):
        return "a" * 40

    verify_components: list[str] = []

    def fake_run(args, **_kwargs):
        component = args[args.index("--component") + 1]
        verify_components.append(component)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "repos": [
                        {
                            "repo": "sure-aio",
                            "component": component,
                            "sha": "a" * 40,
                            "dockerhub": ["wgross19/sure-aio-alpha:latest-alpha"],
                            "ghcr": ["ghcr.io/wgross19/sure-aio-alpha:latest-alpha"],
                            "failures": [],
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(workflow_jobs, "_checkout_refs", fake_checkout_refs)
    monkeypatch.setattr(
        workflow_jobs,
        "publish_components_required",
        lambda _repo, *, sha, event: ["aio"],
    )
    monkeypatch.setattr(workflow_jobs.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)
    report = registry_audit_checkouts(
        manifest_path=manifest,
        checkout_root=checkout_root,
        output_path=output,
        token="token",  # nosec B106
        github_output=None,
    )

    assert verify_components == ["aio"]  # nosec B101
    assert report["status"] == 0  # nosec B101
    rows = {row["component"] for row in report["repos"]}
    assert "aio" in rows  # nosec B101
    assert "sure-alpha" not in rows  # nosec B101


def test_registry_audit_reports_publish_path_resolution_errors_and_continues(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "fleet.yml"
    checkout_root = tmp_path / "checkouts"
    output = tmp_path / "registry-report.json"
    github_output = tmp_path / "github-output.txt"
    manifest.write_text("""
owner: wgross19
repos:
  penpot-aio:
    path: /tmp/penpot-aio
    public: true
    app_slug: penpot-aio
    image_name: wgross19/penpot-aio
    docker_cache_scope: penpot-aio-image
    pytest_image_tag: penpot-aio:pytest
  sure-aio:
    path: /tmp/sure-aio
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio-image
    pytest_image_tag: sure-aio:pytest
""")

    def fake_checkout_refs(refs, *, token: str, submodules: str):
        del token
        assert submodules == "none"  # nosec B101
        results = []
        for name, github_repo, path in refs:
            path.mkdir(parents=True)
            results.append(
                {"repo": name, "github_repo": github_repo, "path": str(path)}
            )
        return results

    def fake_publish_components_required(repo_config, *, sha: str, event: str):
        assert sha == "a" * 40  # nosec B101
        assert event == "push"  # nosec B101
        if repo_config.name == "penpot-aio":
            raise workflow_jobs.PublishPathResolutionError(
                "penpot-aio: unable to resolve changed files for "
                "1613d1732d8ad344325ea0e15defbce9f30f4a14"
            )
        return ["aio"]

    verify_repos: list[str] = []

    def fake_run(args, **_kwargs):
        repo = args[args.index("--repo") + 1]
        component = args[args.index("--component") + 1]
        verify_repos.append(repo)
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "repos": [
                        {
                            "repo": repo,
                            "component": component,
                            "sha": "a" * 40,
                            "dockerhub": [f"wgross19/{repo}:latest"],
                            "ghcr": [f"ghcr.io/wgross19/{repo}:latest"],
                            "failures": [],
                        }
                    ]
                }
            ),
            "",
        )

    monkeypatch.setattr(workflow_jobs, "_checkout_refs", fake_checkout_refs)
    monkeypatch.setattr(
        workflow_jobs, "publish_components_required", fake_publish_components_required
    )
    monkeypatch.setattr(
        workflow_jobs.subprocess,
        "check_output",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)

    report = registry_audit_checkouts(
        manifest_path=manifest,
        checkout_root=checkout_root,
        output_path=output,
        token="token",  # nosec B106
        github_output=github_output,
    )

    assert report["status"] == 1  # nosec B101
    assert verify_repos == ["sure-aio"]  # nosec B101
    rows = {(row["repo"], row.get("component")): row for row in report["repos"]}
    penpot = rows[("penpot-aio", "repo")]
    assert penpot["sha"] == "a" * 40  # nosec B101
    assert "unable to resolve changed files" in penpot["failures"][0]  # nosec B101
    assert rows[("sure-aio", "aio")]["failures"] == []  # nosec B101
    assert json.loads(output.read_text())["status"] == 1  # nosec B101
    assert "status=1" in github_output.read_text()  # nosec B101


def test_checkout_upstream_monitor_repos_writes_token_checkout_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "fleet.yml"
    checkout_root = tmp_path / "checkouts"
    output_manifest = tmp_path / "upstream.manifest.yml"
    output = tmp_path / "checkout-report.json"
    manifest.write_text("""
owner: wgross19
repos:
  sure-aio:
    path: /tmp/sure-aio
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio
    pytest_image_tag: sure-aio:pytest
""")
    observed_refs: list[tuple[str, str, Path]] = []
    observed_token = ""

    def fake_checkout_refs(refs, *, token: str, submodules: str):
        nonlocal observed_token
        observed_refs.extend(refs)
        observed_token = token
        assert submodules == "required"  # nosec B101
        results = []
        for name, github_repo, path in refs:
            path.mkdir(parents=True)
            results.append(
                {"repo": name, "github_repo": github_repo, "path": str(path)}
            )
        return results

    monkeypatch.setattr(workflow_jobs, "_checkout_refs", fake_checkout_refs)

    report = workflow_jobs.checkout_upstream_monitor_repos(
        manifest_path=manifest,
        checkout_root=checkout_root,
        output_manifest=output_manifest,
        output_path=output,
        token="checkout-token",  # nosec B106
    )

    assert observed_token == "checkout-token"  # nosec B101
    assert observed_refs == [  # nosec B101
        ("sure-aio", "wgross19/sure-aio", checkout_root / "sure-aio")
    ]
    assert report["repos"] == [  # nosec B101
        {"repo": "sure-aio", "path": str(checkout_root / "sure-aio")}
    ]
    assert "path: " in output_manifest.read_text()  # nosec B101
    assert (
        json.loads(output.read_text())["repos"][0]["repo"] == "sure-aio"
    )  # nosec B101


def test_registry_audit_handles_publish_path_resolution_error(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "fleet.yml"
    checkout_root = tmp_path / "checkouts"
    output = tmp_path / "registry-report.json"
    manifest.write_text("""
owner: wgross19
repos:
  sure-aio:
    path: /tmp/sure-aio
    public: true
    app_slug: sure-aio
    image_name: wgross19/sure-aio
    docker_cache_scope: sure-aio
    pytest_image_tag: sure-aio:pytest
""")

    def fake_checkout_refs(refs, *, token: str, submodules: str):
        results = []
        for name, github_repo, path in refs:
            path.mkdir(parents=True)
            results.append(
                {"repo": name, "github_repo": github_repo, "path": str(path)}
            )
        return results

    monkeypatch.setattr(workflow_jobs, "_checkout_refs", fake_checkout_refs)
    monkeypatch.setattr(
        workflow_jobs,
        "publish_components_required",
        lambda _repo, *, sha, event: (_ for _ in ()).throw(
            PublishPathResolutionError("sure-aio: unable to resolve changed files")
        ),
    )
    monkeypatch.setattr(
        workflow_jobs.subprocess, "check_output", lambda *_a, **_k: "a" * 40
    )

    report = registry_audit_checkouts(
        manifest_path=manifest,
        checkout_root=checkout_root,
        output_path=output,
        token="token",  # nosec B106
        github_output=None,
    )

    assert report["status"] == 1  # nosec B101
    assert report["repos"] == [  # nosec B101
        {
            "repo": "sure-aio",
            "component": "repo",
            "sha": "a" * 40,
            "dockerhub": [],
            "ghcr": [],
            "failures": ["sure-aio: unable to resolve changed files"],
        }
    ]


def test_upstream_monitor_checkouts_rejects_secret_bearing_launcher(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    output = tmp_path / "upstream-report.json"

    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("AIO_FLEET_WORKFLOW_TOKEN", "workflow")

    try:
        workflow_jobs.upstream_monitor_checkouts(
            manifest_path=manifest,
            output_path=output,
            mutate=True,
            dry_run=False,
        )
    except RuntimeError as error:
        assert "secret-bearing process" in str(error)  # nosec B101
        assert "AIO_FLEET_WORKFLOW_TOKEN" in str(error)  # nosec B101
    else:
        raise AssertionError("expected secret-bearing launcher refusal")


def test_upstream_monitor_checkouts_sanitizes_subprocess_tokens(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    output = tmp_path / "upstream-report.json"
    observed_env: dict[str, str] = {}

    def fake_run(args, **kwargs):
        assert args[:3] == [
            workflow_jobs.sys.executable,
            "-m",
            "aio_fleet",
        ]  # nosec B101
        env = kwargs.get("env")
        assert isinstance(env, dict)  # nosec B101
        observed_env.update(env)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"repos": [{"repo": "sure-aio", "results": []}]}), ""
        )

    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("SAFE_TEST_KEY", "present")

    report = workflow_jobs.upstream_monitor_checkouts(
        manifest_path=manifest,
        output_path=output,
        mutate=False,
        dry_run=False,
    )

    assert report["status"] == 0  # nosec B101
    assert observed_env.get("SAFE_TEST_KEY") == "present"  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in observed_env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in observed_env  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in observed_env  # nosec B101
    assert "DOCKERHUB_TOKEN" not in observed_env  # nosec B101
    assert "CUSTOM_WEBHOOK_URL" not in observed_env  # nosec B101
    assert "APP_TOKEN" not in observed_env  # nosec B101
    assert "GH_TOKEN" not in observed_env  # nosec B101
    assert "GITHUB_TOKEN" not in observed_env  # nosec B101


def test_upstream_monitor_checkouts_keeps_mutation_child_tokenless(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    output = tmp_path / "upstream-report.json"
    observed_env: dict[str, str] = {}
    observed_args: list[str] = []

    def fake_run(args, **kwargs):
        observed_args.extend(args)
        env = kwargs.get("env")
        assert isinstance(env, dict)  # nosec B101
        observed_env.update(env)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"repos": [{"repo": "sure-aio", "results": []}]}), ""
        )

    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("SAFE_TEST_KEY", "present")

    report = workflow_jobs.upstream_monitor_checkouts(
        manifest_path=manifest,
        output_path=output,
        mutate=True,
        dry_run=False,
    )

    assert report["status"] == 0  # nosec B101
    assert observed_env.get("SAFE_TEST_KEY") == "present"  # nosec B101
    assert "--write" in observed_args  # nosec B101
    assert "--create-pr" not in observed_args  # nosec B101
    assert "--post-check" not in observed_args  # nosec B101
    assert "AIO_FLEET_UPSTREAM_TOKEN" not in observed_env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in observed_env  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in observed_env  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in observed_env  # nosec B101
    assert "DOCKERHUB_TOKEN" not in observed_env  # nosec B101
    assert "CUSTOM_WEBHOOK_URL" not in observed_env  # nosec B101
    assert "APP_TOKEN" not in observed_env  # nosec B101
    assert "GH_TOKEN" not in observed_env  # nosec B101
    assert "GITHUB_TOKEN" not in observed_env  # nosec B101


def test_apply_upstream_monitor_actions_creates_pr_from_trusted_parent(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    dockerfile = checkout / "Dockerfile"
    dockerfile.write_text("ARG UPSTREAM_VERSION=1.0.1\n")
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report_path = tmp_path / "upstream-report.json"
    output = tmp_path / "upstream-report.json"
    observed_parent: dict[str, object] = {}

    def fake_create_or_update_upstream_pr(
        repo, results, *, dry_run: bool, post_check: bool
    ):
        observed_parent["repo_path"] = repo.path
        observed_parent["components"] = [result.component for result in results]
        observed_parent["dry_run"] = dry_run
        observed_parent["post_check"] = post_check
        return {"repo": repo.name, "action": "upserted-pr", "branch": "codex/test"}

    monkeypatch.setattr(
        workflow_jobs,
        "create_or_update_upstream_pr",
        fake_create_or_update_upstream_pr,
    )
    monkeypatch.setattr(workflow_jobs, "_changed_paths", lambda _path: {"Dockerfile"})
    monkeypatch.setenv("AIO_FLEET_WORKFLOW_TOKEN", "workflow")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check")
    report_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "results": [_upstream_update_payload(checkout)],
                        "actions": [],
                    }
                ],
                "status": 0,
            }
        )
    )

    report = workflow_jobs.apply_upstream_monitor_actions(
        manifest_path=manifest,
        checkout_root=tmp_path / "checkouts",
        report_path=report_path,
        output_path=output,
    )

    assert report["status"] == 0  # nosec B101
    assert observed_parent == {  # nosec B101
        "repo_path": checkout,
        "components": ["aio"],
        "dry_run": False,
        "post_check": True,
    }
    actions = report["repos"][0]["actions"]
    assert actions == [  # nosec B101
        {"repo": "sure-aio", "action": "upserted-pr", "branch": "codex/test"}
    ]


def test_validate_upstream_monitor_report_normalizes_handoff(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    (checkout / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.1\n")
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report_path = tmp_path / "upstream-report.json"
    output = tmp_path / "validated-report.json"
    report_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "results": [_upstream_update_payload(checkout)],
                        "actions": [],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(workflow_jobs, "_changed_paths", lambda _path: {"Dockerfile"})

    report = workflow_jobs.validate_upstream_monitor_report(
        manifest_path=manifest,
        checkout_root=tmp_path / "checkouts",
        report_path=report_path,
        output_path=output,
    )

    assert report["status"] == 0  # nosec B101
    assert report["repos"][0]["actions"] == []  # nosec B101
    assert report["repos"][0]["results"][0]["latest_version"] == "1.0.1"  # nosec B101
    assert json.loads(output.read_text()) == report  # nosec B101


def test_apply_upstream_monitor_actions_rejects_untrusted_child_actions(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    (checkout / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.1\n")
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report_path = tmp_path / "upstream-report.json"
    output = tmp_path / "upstream-report.json"
    report_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "results": [_upstream_update_payload(checkout)],
                        "actions": [{"action": "upserted-pr"}],
                    }
                ]
            }
        )
    )

    report = workflow_jobs.apply_upstream_monitor_actions(
        manifest_path=manifest,
        checkout_root=tmp_path / "checkouts",
        report_path=report_path,
        output_path=output,
    )

    assert report["status"] == 1  # nosec B101
    assert (
        "refusing untrusted child actions" in report["repos"][0]["error"]
    )  # nosec B101


def test_apply_upstream_monitor_actions_rejects_unexpected_diff(
    monkeypatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkouts" / "sure-aio"
    checkout.mkdir(parents=True)
    (checkout / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.0.1\n")
    manifest = _write_upstream_workflow_manifest(tmp_path, checkout)
    report_path = tmp_path / "upstream-report.json"
    output = tmp_path / "upstream-report.json"
    report_path.write_text(
        json.dumps(
            {
                "repos": [
                    {
                        "repo": "sure-aio",
                        "results": [_upstream_update_payload(checkout)],
                        "actions": [],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        workflow_jobs, "_changed_paths", lambda _path: {"Dockerfile", "evil.sh"}
    )

    report = workflow_jobs.apply_upstream_monitor_actions(
        manifest_path=manifest,
        checkout_root=tmp_path / "checkouts",
        report_path=report_path,
        output_path=output,
    )

    assert report["status"] == 1  # nosec B101
    assert (
        "unexpected upstream monitor changes" in report["repos"][0]["error"]
    )  # nosec B101


def test_checkout_refs_uses_bounded_single_branch_clone(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(workflow_jobs.subprocess, "run", fake_run)

    checkout = tmp_path / "checkouts" / "sure-aio"
    results = workflow_jobs._checkout_refs(
        [("sure-aio", "wgross19/sure-aio", checkout)],
        token="token",  # nosec B106
        submodules="none",
    )

    assert len(results) == 1  # nosec B101
    assert calls[0][:5] == [  # nosec B101
        "git",
        "clone",
        "--single-branch",
        "--filter=blob:none",
        "https://github.com/wgross19/sure-aio.git",
    ]
