from __future__ import annotations

import subprocess  # nosec B404
import sys
from pathlib import Path

from aio_fleet.control_plane import (
    Step,
    central_check_steps,
    registry_publish_command,
    run_central_trunk,
    run_steps,
)
from aio_fleet.manifest import RepoConfig, load_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_central_check_steps_for_pr_include_policy_and_integration(
    tmp_path: Path,
) -> None:
    repo = _repo_with_path(load_manifest(ROOT / "fleet.yml").repo("sure-aio"), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "aio_fleet").mkdir()
    (tmp_path / "aio_fleet" / "__init__.py").write_text("")
    (tmp_path / "aio_fleet" / "cli.py").write_text("raise SystemExit(0)\n")

    steps = central_check_steps(repo, event="pull_request", include_trunk=False)

    names = [step.name for step in steps]
    assert names == [
        "validate-repo",
        "verify-caller",
        "install-test-deps",
        "integration-tests",
    ]  # nosec B101
    assert steps[0].cwd == ROOT  # nosec B101
    assert steps[1].cwd == ROOT  # nosec B101
    assert steps[2].cwd == tmp_path  # nosec B101
    assert str(ROOT) in steps[2].command[-1]  # nosec B101
    assert steps[2].command[-1].endswith("[app-tests]")  # nosec B101


def test_central_check_steps_keep_pr_integration_when_submodule_checkout_is_missing(
    tmp_path: Path,
) -> None:
    repo = _repo_with_path(load_manifest(ROOT / "fleet.yml").repo("mem0-aio"), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "openmemory").mkdir()

    steps = central_check_steps(repo, event="pull_request", include_trunk=False)

    names = [step.name for step in steps]
    assert "integration-tests" in names  # nosec B101


def test_central_check_steps_keep_push_integration_for_submodule_repos(
    tmp_path: Path,
) -> None:
    repo = _repo_with_path(load_manifest(ROOT / "fleet.yml").repo("mem0-aio"), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "openmemory").mkdir()

    steps = central_check_steps(repo, event="push", include_trunk=False)

    names = [step.name for step in steps]
    assert "integration-tests" in names  # nosec B101


def test_central_check_steps_use_trusted_python_for_app_tests(
    tmp_path: Path,
) -> None:
    repo = _repo_with_path(load_manifest(ROOT / "fleet.yml").repo("sure-aio"), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "bin").mkdir()
    malicious_python = tmp_path / "bin" / "python"
    malicious_python.write_text("#!/bin/sh\nexit 0\n")
    malicious_python.chmod(0o755)
    (tmp_path / ".venv-local").symlink_to(".", target_is_directory=True)

    steps = central_check_steps(repo, event="pull_request", include_trunk=False)

    install = [step for step in steps if step.name == "install-test-deps"][0]
    integration = [step for step in steps if step.name == "integration-tests"][0]
    assert install.command[:4] == [  # nosec B101
        sys.executable,
        "-m",
        "pip",
        "install",
    ]
    assert integration.command[0] == sys.executable  # nosec B101
    assert str(malicious_python) not in install.command  # nosec B101
    assert str(malicious_python) not in integration.command  # nosec B101


def test_central_check_steps_for_push_include_integration_trunk_and_publish() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("sure-aio")

    steps = central_check_steps(
        repo, event="push", publish=True, publish_component_names=["aio"]
    )

    names = [step.name for step in steps]
    assert "registry-publish-preflight" in names  # nosec B101
    assert "build-pytest-image" in names  # nosec B101
    assert "integration-tests" in names  # nosec B101
    assert names[-2:] == ["trunk", "registry-publish"]  # nosec B101
    preflight = steps[names.index("registry-publish-preflight")]
    assert preflight.cwd == ROOT  # nosec B101
    assert preflight.inherit_secrets is True  # nosec B101
    assert preflight.command[-4:] == [  # nosec B101
        "--mode",
        "publish",
        "--format",
        "json",
    ]
    build = steps[names.index("build-pytest-image")]
    assert build.stream_output is True  # nosec B101
    assert build.timeout_seconds == 1800  # nosec B101
    assert build.inherit_secrets is False  # nosec B101
    assert build.command[:6] == [  # nosec B101
        "docker",
        "build",
        "--progress=plain",
        "--platform",
        "linux/amd64",
        "-t",
    ]
    integration = steps[names.index("integration-tests")]
    assert integration.env == {"AIO_PYTEST_USE_PREBUILT_IMAGE": "true"}  # nosec B101
    assert integration.timeout_seconds == 1800  # nosec B101
    publish = steps[names.index("registry-publish")]
    assert publish.stream_output is True  # nosec B101
    assert publish.timeout_seconds == 3600  # nosec B101
    assert publish.inherit_secrets is True  # nosec B101
    trunk = steps[names.index("trunk")]
    assert trunk.inherit_secrets is False  # nosec B101


def test_central_check_steps_validation_only_keeps_publish_context() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("sure-aio")

    steps = central_check_steps(
        repo,
        event="push",
        publish=True,
        publish_component_names=["aio"],
        include_publish_steps=False,
    )

    names = [step.name for step in steps]
    assert "build-pytest-image" in names  # nosec B101
    assert "integration-tests" in names  # nosec B101
    assert "trunk" in names  # nosec B101
    assert "registry-publish-preflight" not in names  # nosec B101
    assert "registry-publish" not in names  # nosec B101


def test_central_check_steps_publish_only_skips_app_code() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("sure-aio")

    steps = central_check_steps(
        repo,
        event="push",
        publish=True,
        publish_component_names=["aio"],
        include_app_checks=False,
        include_github_prereleases=False,
    )

    names = [step.name for step in steps]
    assert names == ["registry-publish-preflight", "registry-publish"]  # nosec B101
    assert all(step.inherit_secrets is True for step in steps)  # nosec B101


def test_central_check_steps_use_mem0_publish_timeout() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("mem0-aio")

    steps = central_check_steps(
        repo, event="push", publish=True, include_integration=False
    )

    publish = [step for step in steps if step.name == "registry-publish"][0]
    assert publish.timeout_seconds == 7200  # nosec B101


def test_central_check_steps_skip_template_registry_publish() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("unraid-aio-template")

    steps = central_check_steps(repo, event="push", publish=True)

    names = [step.name for step in steps]
    assert "build-pytest-image" not in names  # nosec B101
    assert not any(name.startswith("registry-publish") for name in names)  # nosec B101
    assert "integration-tests" in names  # nosec B101


def test_registry_publish_command_uses_plain_progress() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("mem0-aio")

    command = registry_publish_command(repo, sha="a" * 40)

    assert command[:4] == [  # nosec B101
        "docker",
        "buildx",
        "build",
        "--progress=plain",
    ]
    assert "--attest=type=provenance,mode=max" in command  # nosec B101
    assert "--attest=type=sbom" in command  # nosec B101


def test_central_check_steps_for_push_without_publish_lets_tests_build_image() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("sure-aio")

    steps = central_check_steps(repo, event="push", publish=False)

    names = [step.name for step in steps]
    assert "build-pytest-image" not in names  # nosec B101
    integration = steps[names.index("integration-tests")]
    assert integration.env is None  # nosec B101


def test_central_check_steps_publish_signoz_components() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("signoz-aio")

    steps = central_check_steps(repo, event="workflow_dispatch", publish=True)

    publish_steps = [
        step
        for step in steps
        if step.name.startswith("registry-publish-")
        and step.name != "registry-publish-preflight"
    ]
    assert "registry-publish-preflight" in [step.name for step in steps]  # nosec B101
    assert [step.name for step in publish_steps] == [  # nosec B101
        "registry-publish-aio",
        "registry-publish-agent",
    ]
    assert publish_steps[1].command[-2:] == ["--component", "agent"]  # nosec B101


def test_central_check_steps_can_target_alpha_component_publish() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("sure-aio")

    steps = central_check_steps(
        repo,
        event="push",
        publish=True,
        publish_component_names=["sure-alpha"],
    )

    names = [step.name for step in steps]
    assert "build-pytest-image-sure-alpha" in names  # nosec B101
    assert "registry-publish-preflight" in names  # nosec B101
    assert "registry-publish-sure-alpha" in names  # nosec B101
    assert "github-prerelease-sure-alpha" in names  # nosec B101
    assert "registry-publish-aio" not in names  # nosec B101
    build = steps[names.index("build-pytest-image-sure-alpha")]
    assert (
        build.command[build.command.index("-t") + 1] == "sure-aio-alpha:pytest"
    )  # nosec B101
    assert (
        build.command[build.command.index("-f") + 1] == "Dockerfile.alpha"
    )  # nosec B101
    integration = steps[names.index("integration-tests")]
    assert integration.env == {  # nosec B101
        "AIO_ALPHA_PYTEST_USE_PREBUILT_IMAGE": "true"
    }
    publish = steps[names.index("registry-publish-sure-alpha")]
    assert publish.command[-2:] == ["--component", "sure-alpha"]  # nosec B101
    release = steps[names.index("github-prerelease-sure-alpha")]
    assert release.command[-2:] == ["--component", "sure-alpha"]  # nosec B101
    assert release.stream_output is False  # nosec B101


def test_signoz_agent_pytest_build_uses_component_context() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("signoz-aio")

    steps = central_check_steps(
        repo,
        event="push",
        publish=True,
        publish_component_names=["agent"],
    )

    names = [step.name for step in steps]
    build = steps[names.index("build-pytest-image-agent")]
    assert (  # nosec B101
        build.command[build.command.index("-f") + 1]
        == "components/signoz-agent/Dockerfile"
    )
    assert build.command[-1] == "components/signoz-agent"  # nosec B101


def test_central_check_steps_can_skip_integration_for_poll_runs() -> None:
    repo = load_manifest(ROOT / "fleet.yml").repo("mem0-aio")

    steps = central_check_steps(repo, event="push", include_integration=False)

    names = [step.name for step in steps]
    assert "integration-tests" not in names  # nosec B101
    assert "unit-tests" in names  # nosec B101
    assert "trunk" in names  # nosec B101
    assert names[:2] == ["validate-repo", "verify-caller"]  # nosec B101


def test_run_steps_reports_timeout(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(cmd=["slow"], timeout=5)

    monkeypatch.setattr(subprocess, "run", fake_run)

    failures = run_steps(
        [Step("slow-step", ["slow"], tmp_path, timeout_seconds=5)],
        dry_run=False,
    )

    assert failures == ["slow-step: timed out after 5s"]  # nosec B101


def test_run_steps_decodes_timeout_output(monkeypatch, tmp_path: Path, capsys) -> None:
    def fake_run(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(
            cmd=["slow"],
            timeout=5,
            output=b"partial stdout\n",
            stderr=b"partial stderr\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    failures = run_steps(
        [Step("slow-step", ["slow"], tmp_path, timeout_seconds=5)],
        dry_run=False,
    )
    captured = capsys.readouterr()

    assert failures == ["slow-step: timed out after 5s"]  # nosec B101
    assert captured.out == "partial stdout\n"  # nosec B101
    assert captured.err == "partial stderr\n"  # nosec B101
    assert "b'partial" not in captured.out + captured.err  # nosec B101


def test_run_steps_includes_failure_detail(monkeypatch, tmp_path: Path) -> None:
    def fake_run(*_args: object, **_kwargs: object):
        return subprocess.CompletedProcess(
            ["release"],
            1,
            "stdout context\n",
            "first stderr line\nretarget immutable release\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    failures = run_steps([Step("release-step", ["release"], tmp_path)], dry_run=False)

    assert failures == [  # nosec B101
        "release-step: exit 1: retarget immutable release"
    ]


def test_run_steps_prefers_actionable_failure_detail(
    monkeypatch, tmp_path: Path
) -> None:
    def fake_run(*_args: object, **_kwargs: object):
        return subprocess.CompletedProcess(
            ["trunk"],
            1,
            "Checked 1 file\nIncorrect formatting\nTrunk is now managing hooks\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    failures = run_steps([Step("trunk", ["trunk"], tmp_path)], dry_run=False)

    assert failures == ["trunk: exit 1: Incorrect formatting"]  # nosec B101


def test_run_steps_scrubs_secret_environment_for_untrusted_steps(
    monkeypatch, tmp_path: Path
) -> None:
    captured_env: dict[str, str] | None = None

    def fake_run(*_args: object, **kwargs: object):
        nonlocal captured_env
        captured_env = kwargs["env"]
        return subprocess.CompletedProcess(["probe"], 0, "", "")

    monkeypatch.setenv("AIO_FLEET_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check-token")
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("SAFE_ENV", "safe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    failures = run_steps(
        [
            Step(
                "safe-step",
                ["probe"],
                tmp_path,
                env={"AIO_PYTEST_USE_PREBUILT_IMAGE": "true"},
                inherit_secrets=False,
            )
        ],
        dry_run=False,
    )

    assert failures == []  # nosec B101
    assert captured_env is not None  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in captured_env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in captured_env  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_ENV" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101
    assert captured_env["SAFE_ENV"] == "safe"  # nosec B101
    assert captured_env["AIO_PYTEST_USE_PREBUILT_IMAGE"] == "true"  # nosec B101


def test_run_central_trunk_scrubs_secret_environment(
    monkeypatch, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    captured_env: dict[str, str] | None = None

    def fake_run(command: list[str], **kwargs: object):
        nonlocal captured_env
        if "clone" in command:
            Path(command[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        captured_env = kwargs["env"]  # type: ignore[assignment]
        return subprocess.CompletedProcess(command, 0, "", "")

    repo = _repo_with_path(
        load_manifest(ROOT / "fleet.yml").repo("sure-aio"), repo_path
    )
    monkeypatch.setenv("TRUNK_PATH", str(tmp_path / "trunk"))
    monkeypatch.setenv("AIO_FLEET_TMPDIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("AIO_FLEET_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check-token")
    monkeypatch.setenv("AIO_FLEET_RELEASE_TOKEN", "release-token")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "dockerhub-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("SAFE_ENV", "safe")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_central_trunk(repo)

    assert result.returncode == 0  # nosec B101
    assert captured_env is not None  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in captured_env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in captured_env  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" not in captured_env  # nosec B101
    assert "DOCKERHUB_TOKEN" not in captured_env  # nosec B101
    assert "GH_TOKEN" not in captured_env  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101
    assert captured_env["SAFE_ENV"] == "safe"  # nosec B101


def _repo_with_path(repo: RepoConfig, path: Path) -> RepoConfig:
    raw = dict(repo.raw)
    raw["path"] = str(path)
    return RepoConfig(name=repo.name, raw=raw, defaults=repo.defaults, owner=repo.owner)
