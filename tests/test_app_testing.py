from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from aio_fleet import app_testing


def _completed(
    stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout=stdout, stderr="")


def test_configure_repo_root_updates_default_command_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    repo_root = Path("/workspace/app")

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return _completed()

    monkeypatch.setattr(app_testing.subprocess, "run", fake_run)
    app_testing.configure_repo_root(repo_root)

    app_testing.run_command(["python", "-m", "pytest"])

    assert calls == [
        {
            "args": ["python", "-m", "pytest"],
            "cwd": repo_root,
            "env": None,
            "check": True,
            "text": True,
            "capture_output": True,
        }
    ]


def test_default_runtime_builds_template_container_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    removed_containers: list[str] = []
    removed_volumes: list[str] = []
    ports = iter([18080])

    monkeypatch.setattr(
        app_testing.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    monkeypatch.setattr(app_testing, "reserve_host_port", lambda: next(ports))
    monkeypatch.setattr(
        app_testing,
        "create_docker_volume",
        lambda prefix: f"{prefix}-volume",
    )
    monkeypatch.setattr(
        app_testing,
        "remove_docker_volume",
        lambda volume_name: removed_volumes.append(volume_name),
    )

    def fake_run_command(
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:3] == ["docker", "rm", "-f"]:
            removed_containers.append(command[3])
        return _completed()

    monkeypatch.setattr(app_testing, "run_command", fake_run_command)

    runtime = app_testing.DockerRuntime("example/app:pytest")
    with runtime.container(env_overrides={"APP_MODE": "test"}) as handle:
        assert handle.name == "aio-template-pytest-abcdef1234"
        assert handle.http_port == 18080
        assert handle.config_volume == "aio-template-pytest-abcdef1234-config-volume"
        assert handle.data_volume == "aio-template-pytest-abcdef1234-data-volume"

    assert commands[0] == [
        "docker",
        "run",
        "-d",
        "--platform",
        "linux/amd64",
        "--name",
        "aio-template-pytest-abcdef1234",
        "-p",
        "18080:8080",
        "-v",
        "aio-template-pytest-abcdef1234-config-volume:/config",
        "-v",
        "aio-template-pytest-abcdef1234-data-volume:/data",
        "-e",
        "APP_MODE=test",
        "example/app:pytest",
    ]
    assert removed_containers == ["aio-template-pytest-abcdef1234"]
    assert removed_volumes == [
        "aio-template-pytest-abcdef1234-config-volume",
        "aio-template-pytest-abcdef1234-data-volume",
    ]


def test_runtime_can_use_external_appdata_volume_without_removing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    removed_volumes: list[str] = []
    ports = iter([18080])

    monkeypatch.setattr(
        app_testing.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="0123456789abcdef"),
    )
    monkeypatch.setattr(app_testing, "reserve_host_port", lambda: next(ports))
    monkeypatch.setattr(
        app_testing,
        "remove_docker_volume",
        lambda volume_name: removed_volumes.append(volume_name),
    )
    monkeypatch.setattr(
        app_testing,
        "run_command",
        lambda command, **_: commands.append(command) or _completed(),
    )

    runtime = app_testing.DockerRuntime(
        "example/dify:pytest",
        name_prefix="dify-aio-pytest",
        default_env={"PUBLIC_URL": "http://127.0.0.1:{http_port}"},
        volume_mounts=(
            app_testing.VolumeMount("appdata_volume", "/appdata", "appdata"),
        ),
        exec_clears_proxy_env=True,
    )

    with runtime.container(
        appdata_volume="existing-appdata",
        network="dify-net",
        extra_args=["--add-host", "host.docker.internal:host-gateway"],
    ) as handle:
        assert handle.appdata_volume == "existing-appdata"

    assert commands[0] == [
        "docker",
        "run",
        "-d",
        "--platform",
        "linux/amd64",
        "--name",
        "dify-aio-pytest-0123456789",
        "--network",
        "dify-net",
        "-p",
        "18080:8080",
        "-v",
        "existing-appdata:/appdata",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-e",
        "PUBLIC_URL=http://127.0.0.1:18080",
        "example/dify:pytest",
    ]
    assert removed_volumes == []


def test_runtime_preseeds_appdata_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    ports = iter([18080])

    monkeypatch.setattr(
        app_testing.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="fedcba9876543210"),
    )
    monkeypatch.setattr(app_testing, "reserve_host_port", lambda: next(ports))
    monkeypatch.setattr(
        app_testing,
        "create_docker_volume",
        lambda prefix: f"{prefix}-volume",
    )
    monkeypatch.setattr(app_testing, "remove_docker_volume", lambda _: None)
    monkeypatch.setattr(
        app_testing,
        "run_command",
        lambda command, **_: commands.append(command) or _completed(),
    )

    runtime = app_testing.DockerRuntime(
        "example/penpot:pytest",
        name_prefix="penpot-aio-pytest",
        volume_mounts=(
            app_testing.VolumeMount("appdata_volume", "/appdata", "appdata"),
        ),
    )

    with runtime.container(preseed_appdata=["touch /appdata/seeded"]):
        pass

    assert commands[0] == [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--entrypoint",
        "sh",
        "-v",
        "penpot-aio-pytest-fedcba9876-appdata-volume:/appdata",
        "example/penpot:pytest",
        "-lc",
        "touch /appdata/seeded",
    ]


def test_docker_exec_can_strip_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        app_testing,
        "run_command",
        lambda command, **_: commands.append(command) or _completed(),
    )

    app_testing.configure_docker_exec(clear_proxy_env=True)
    try:
        app_testing.docker_exec("container", "python --version")
    finally:
        app_testing.configure_docker_exec()

    assert commands == [
        [
            "docker",
            "exec",
            "container",
            *app_testing.DOCKER_EXEC_PROXY_ENV_ARGS,
            "sh",
            "-lc",
            "python --version",
        ]
    ]


def test_ensure_pytest_image_honors_prebuilt_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIO_PYTEST_USE_PREBUILT_IMAGE", "true")
    monkeypatch.setattr(app_testing, "docker_image_exists", lambda _: False)

    with pytest.raises(AssertionError, match="Expected prebuilt pytest image"):
        app_testing.ensure_pytest_image("missing:image")


def test_sidecar_container_builds_and_removes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        app_testing.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="1122334455667788"),
    )
    monkeypatch.setattr(
        app_testing,
        "run_command",
        lambda command, **_: commands.append(command) or _completed(),
    )

    with app_testing.sidecar_container(
        "postgres",
        "postgres:16-alpine",
        network="app-net",
        network_alias="db",
        env={"POSTGRES_PASSWORD": "example"},
        ports={15432: 5432},
    ) as name:
        assert name == "postgres-1122334455"

    assert commands == [
        [
            "docker",
            "run",
            "-d",
            "--name",
            "postgres-1122334455",
            "--platform",
            "linux/amd64",
            "--network",
            "app-net",
            "--network-alias",
            "db",
            "-p",
            "127.0.0.1:15432:5432",
            "-e",
            "POSTGRES_PASSWORD=example",
            "postgres:16-alpine",
        ],
        ["docker", "rm", "-f", "postgres-1122334455"],
    ]
