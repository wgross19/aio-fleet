from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess  # nosec B404 - app tests shell out to trusted local tooling.
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(os.environ.get("AIO_FLEET_APP_REPO_ROOT", Path.cwd())).resolve()

__all__ = [
    "BindMount",
    "ContainerHandle",
    "DEFAULT_TEMPLATE_PORTS",
    "DEFAULT_TEMPLATE_VOLUMES",
    "DOCKER_EXEC_PROXY_ENV_ARGS",
    "DockerRuntime",
    "PortMapping",
    "REPO_ROOT",
    "VolumeMount",
    "configure_docker_exec",
    "configure_repo_root",
    "container_file_size",
    "container_path_exists",
    "create_docker_network",
    "create_docker_volume",
    "docker_available",
    "docker_exec",
    "docker_image_exists",
    "docker_logs",
    "docker_network",
    "docker_volume",
    "ensure_image",
    "ensure_pytest_image",
    "pytest_env",
    "read_container_file",
    "remove_docker_network",
    "remove_docker_volume",
    "reserve_host_port",
    "run_command",
    "sidecar_container",
    "temp_dir",
    "wait_for_container_command",
    "wait_for_host_http",
]

DOCKER_EXEC_PROXY_ENV_ARGS = [
    "env",
    "-u",
    "HTTP_PROXY",
    "-u",
    "HTTPS_PROXY",
    "-u",
    "ALL_PROXY",
    "-u",
    "NO_PROXY",
    "-u",
    "http_proxy",
    "-u",
    "https_proxy",
    "-u",
    "all_proxy",
    "-u",
    "no_proxy",
]

_DOCKER_EXEC_CLEARS_PROXY_ENV = False
_DOCKER_EXEC_SHELL = "sh"


@dataclass(frozen=True)
class PortMapping:
    attr: str
    container_port: int
    host_ip: str | None = None


@dataclass(frozen=True)
class VolumeMount:
    attr: str
    target: str
    prefix_suffix: str
    read_only: bool = False


@dataclass(frozen=True)
class BindMount:
    source: Path | str
    target: str
    read_only: bool = False


DEFAULT_TEMPLATE_PORTS = (PortMapping("http_port", 8080),)
DEFAULT_TEMPLATE_VOLUMES = (
    VolumeMount("config_volume", "/config", "config"),
    VolumeMount("data_volume", "/data", "data"),
)


def configure_repo_root(repo_root: Path | str) -> None:
    global REPO_ROOT
    REPO_ROOT = Path(repo_root).resolve()


def configure_docker_exec(*, clear_proxy_env: bool = False, shell: str = "sh") -> None:
    global _DOCKER_EXEC_CLEARS_PROXY_ENV, _DOCKER_EXEC_SHELL
    _DOCKER_EXEC_CLEARS_PROXY_ENV = clear_proxy_env
    _DOCKER_EXEC_SHELL = shell


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - tests execute trusted local commands.
        command,
        cwd=cwd or REPO_ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False

    return run_command(["docker", "info"], check=False).returncode == 0


def docker_image_exists(image_tag: str) -> bool:
    return (
        run_command(["docker", "image", "inspect", image_tag], check=False).returncode
        == 0
    )


def ensure_pytest_image(
    image_tag: str,
    *,
    context: str = ".",
    dockerfile: str = "Dockerfile",
    platform: str = "linux/amd64",
    prebuilt_env: str = "AIO_PYTEST_USE_PREBUILT_IMAGE",
) -> None:
    if os.environ.get(prebuilt_env) == "true":
        if not docker_image_exists(image_tag):
            raise AssertionError(
                f"Expected prebuilt pytest image {image_tag} to be loaded before the test run."
            )
        return

    command = ["docker", "build", "--platform", platform, "-t", image_tag]
    if dockerfile:
        command.extend(["-f", dockerfile])
    command.append(context)
    run_command(command)


def ensure_image(
    image_tag: str,
    *,
    context: str = ".",
    dockerfile: str = "Dockerfile",
    platform: str = "linux/amd64",
    prebuilt_env: str = "AIO_PYTEST_USE_PREBUILT_IMAGE",
) -> None:
    ensure_pytest_image(
        image_tag,
        context=context,
        dockerfile=dockerfile,
        platform=platform,
        prebuilt_env=prebuilt_env,
    )


def reserve_host_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def create_docker_volume(prefix: str) -> str:
    volume_name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    run_command(["docker", "volume", "create", volume_name])
    return volume_name


def remove_docker_volume(volume_name: str, *, attempts: int = 5) -> None:
    for attempt in range(attempts):
        result = run_command(["docker", "volume", "rm", "-f", volume_name], check=False)
        if result.returncode == 0 or attempt == attempts - 1:
            return
        time.sleep(1)


@contextmanager
def docker_volume(prefix: str) -> Iterator[str]:
    volume_name = create_docker_volume(prefix)
    try:
        yield volume_name
    finally:
        remove_docker_volume(volume_name)


def create_docker_network(prefix: str) -> str:
    network_name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    run_command(["docker", "network", "create", network_name])
    return network_name


def remove_docker_network(network_name: str) -> None:
    run_command(["docker", "network", "rm", network_name], check=False)


@contextmanager
def docker_network(prefix: str) -> Iterator[str]:
    network_name = create_docker_network(prefix)
    try:
        yield network_name
    finally:
        remove_docker_network(network_name)


@contextmanager
def temp_dir(prefix: str) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def docker_exec(
    container_name: str,
    command: str,
    *,
    check: bool = True,
    clear_proxy_env: bool | None = None,
    shell: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if clear_proxy_env is None:
        clear_proxy_env = _DOCKER_EXEC_CLEARS_PROXY_ENV
    shell = shell or _DOCKER_EXEC_SHELL

    exec_command = ["docker", "exec", container_name]
    if clear_proxy_env:
        exec_command.extend(DOCKER_EXEC_PROXY_ENV_ARGS)
    exec_command.extend([shell, "-lc", command])
    return run_command(exec_command, check=check)


def docker_logs(container_name: str) -> str:
    result = run_command(["docker", "logs", container_name], check=False)
    return result.stdout + result.stderr


def wait_for_container_command(
    container_name: str,
    command: str,
    *,
    timeout: int = 120,
    interval: int = 2,
    clear_proxy_env: bool | None = None,
    shell: str | None = None,
) -> None:
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = docker_exec(
            container_name,
            command,
            check=False,
            clear_proxy_env=clear_proxy_env,
            shell=shell,
        )
        if result.returncode == 0:
            return

        running = run_command(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            check=False,
        ).stdout.strip()
        if running == "false":
            raise AssertionError(
                f"{container_name} stopped while waiting for command: {command}\n"
                f"Logs:\n{docker_logs(container_name)}"
            )
        time.sleep(interval)

    raise AssertionError(
        f"{container_name} did not satisfy command before timeout: {command}\n"
        f"Logs:\n{docker_logs(container_name)}"
    )


def wait_for_host_http(url: str, *, timeout: int = 120, interval: int = 2) -> None:
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = run_command(["curl", "-fsS", url], check=False)
        if result.returncode == 0:
            return
        time.sleep(interval)

    raise AssertionError(f"HTTP endpoint did not become ready: {url}")


@contextmanager
def sidecar_container(
    prefix: str,
    image: str,
    *,
    network: str,
    command_args: list[str] | None = None,
    env: dict[str, str] | None = None,
    network_alias: str | None = None,
    platform: str | None = "linux/amd64",
    ports: dict[int, int] | None = None,
) -> Iterator[str]:
    name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    command = ["docker", "run", "-d", "--name", name]

    if platform:
        command.extend(["--platform", platform])

    command.extend(["--network", network])

    if network_alias:
        command.extend(["--network-alias", network_alias])

    if ports:
        for host_port, container_port in ports.items():
            command.extend(["-p", f"127.0.0.1:{host_port}:{container_port}"])

    if env:
        for key, value in env.items():
            command.extend(["-e", f"{key}={value}"])

    command.append(image)
    if command_args:
        command.extend(command_args)

    run_command(command)
    try:
        yield name
    finally:
        run_command(["docker", "rm", "-f", name], check=False)


def container_path_exists(container_name: str, path: str) -> bool:
    return (
        docker_exec(
            container_name,
            f"test -e {shlex.quote(path)}",
            check=False,
        ).returncode
        == 0
    )


def read_container_file(container_name: str, path: str) -> str:
    return docker_exec(container_name, f"cat {shlex.quote(path)}").stdout


def container_file_size(container_name: str, path: str) -> int:
    return int(
        docker_exec(container_name, f"wc -c < {shlex.quote(path)}").stdout.strip()
    )


class DockerRuntime:
    def __init__(
        self,
        image_tag: str,
        *,
        name_prefix: str = "aio-template-pytest",
        platform: str | None = "linux/amd64",
        port_mappings: Sequence[PortMapping] | None = DEFAULT_TEMPLATE_PORTS,
        volume_mounts: Sequence[VolumeMount] | None = DEFAULT_TEMPLATE_VOLUMES,
        default_env: Mapping[str, str] | None = None,
        default_extra_args: Sequence[str] | None = None,
        exec_clears_proxy_env: bool = False,
        exec_shell: str = "sh",
        health_path: str = "/health",
        health_timeout: int = 180,
        appdata_volume_attr: str = "appdata_volume",
        appdata_target: str = "/appdata",
        appdata_env_name: str | None = None,
    ) -> None:
        self.image_tag = image_tag
        self.name_prefix = name_prefix
        self.platform = platform
        self.port_mappings = tuple(port_mappings or ())
        self.volume_mounts = tuple(volume_mounts or ())
        self.default_env = dict(default_env or {})
        self.default_extra_args = list(default_extra_args or [])
        self.exec_clears_proxy_env = exec_clears_proxy_env
        self.exec_shell = exec_shell
        self.health_path = health_path
        self.health_timeout = health_timeout
        self.appdata_volume_attr = appdata_volume_attr
        self.appdata_target = appdata_target
        self.appdata_env_name = appdata_env_name

    def build(self) -> None:
        ensure_pytest_image(self.image_tag)

    def inspect_state(self, name: str, field: str) -> str:
        result = run_command(
            ["docker", "inspect", "-f", f"{{{{.{field}}}}}", name],
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def logs(self, name: str) -> str:
        return docker_logs(name)

    def remove(self, name: str) -> None:
        run_command(["docker", "rm", "-f", name], check=False)

    @contextmanager
    def container(
        self,
        *,
        env_overrides: dict[str, str] | None = None,
        network: str | None = None,
        extra_args: list[str] | None = None,
        appdata_volume: str | None = None,
        preseed_appdata: list[str] | None = None,
        appdata: Path | None = None,
        mount_docker_socket: bool = False,
        bind_mounts: Sequence[BindMount] | None = None,
        command_args: list[str] | None = None,
    ) -> Iterator["ContainerHandle"]:
        suffix = uuid.uuid4().hex[:10]
        name = f"{self.name_prefix}-{suffix}"
        attrs: dict[str, object] = {}
        created_volumes: list[str] = []
        volume_sources: dict[str, str] = {}
        port_values: dict[str, int] = {}

        for mapping in self.port_mappings:
            host_port = reserve_host_port()
            port_values[mapping.attr] = host_port
            attrs[mapping.attr] = host_port

        for mount in self.volume_mounts:
            source = appdata_volume if mount.attr == self.appdata_volume_attr else None
            if source is None:
                source = create_docker_volume(f"{name}-{mount.prefix_suffix}")
                created_volumes.append(source)
            volume_sources[mount.attr] = source
            attrs[mount.attr] = source

        try:
            if preseed_appdata:
                self._preseed_appdata(volume_sources, preseed_appdata)

            command = ["docker", "run", "-d"]
            if self.platform:
                command.extend(["--platform", self.platform])
            command.extend(["--name", name])

            if network:
                command.extend(["--network", network])

            for mapping in self.port_mappings:
                host_port = port_values[mapping.attr]
                published = f"{host_port}:{mapping.container_port}"
                if mapping.host_ip:
                    published = f"{mapping.host_ip}:{published}"
                command.extend(["-p", published])

            for mount in self.volume_mounts:
                suffix = ":ro" if mount.read_only else ""
                command.extend(
                    ["-v", f"{volume_sources[mount.attr]}:{mount.target}{suffix}"]
                )

            if appdata is not None:
                command.extend(["-v", f"{appdata}:{self.appdata_target}"])
                attrs["appdata"] = appdata

            for bind in bind_mounts or ():
                suffix = ":ro" if bind.read_only else ""
                command.extend(["-v", f"{bind.source}:{bind.target}{suffix}"])

            if mount_docker_socket:
                command.extend(["-v", "/var/run/docker.sock:/var/run/docker.sock"])

            command.extend(self.default_extra_args)
            if extra_args:
                command.extend(extra_args)

            env = {
                key: _format_env_value(value, attrs)
                for key, value in self.default_env.items()
            }
            if appdata is not None and self.appdata_env_name:
                env.setdefault(self.appdata_env_name, str(appdata))
            if env_overrides:
                env.update(env_overrides)
            for key, value in env.items():
                command.extend(["-e", f"{key}={value}"])

            command.append(self.image_tag)
            if command_args:
                command.extend(command_args)

            run_command(command)
            handle = ContainerHandle(runtime=self, name=name, **attrs)
            try:
                yield handle
            finally:
                self.remove(name)
        finally:
            for volume_name in created_volumes:
                remove_docker_volume(volume_name)

    def _preseed_appdata(
        self, volume_sources: Mapping[str, str], scripts: list[str]
    ) -> None:
        appdata_volume = volume_sources.get(self.appdata_volume_attr)
        if appdata_volume is None:
            raise ValueError(
                f"preseed_appdata requires a {self.appdata_volume_attr!r} volume mount"
            )

        for script in scripts:
            command = ["docker", "run", "--rm"]
            if self.platform:
                command.extend(["--platform", self.platform])
            command.extend(
                [
                    "--entrypoint",
                    "sh",
                    "-v",
                    f"{appdata_volume}:{self.appdata_target}",
                    self.image_tag,
                    "-lc",
                    script,
                ]
            )
            run_command(command)


class ContainerHandle:
    def __init__(self, *, runtime: DockerRuntime, name: str, **attrs: object) -> None:
        self.runtime = runtime
        self.name = name
        for key, value in attrs.items():
            setattr(self, key, value)

    def logs(self) -> str:
        return self.runtime.logs(self.name)

    def exec(
        self, command: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return docker_exec(
            self.name,
            command,
            check=check,
            clear_proxy_env=self.runtime.exec_clears_proxy_env,
            shell=self.runtime.exec_shell,
        )

    def restart(self) -> None:
        run_command(["docker", "restart", self.name])

    def is_running(self) -> bool:
        return self.runtime.inspect_state(self.name, "State.Status") == "running"

    def path_exists(self, path: str) -> bool:
        return self.exec(f"test -e {shlex.quote(path)}", check=False).returncode == 0

    def read_text(self, path: str) -> str:
        return self.exec(f"cat {shlex.quote(path)}").stdout

    def read_file(self, path: str) -> str:
        return self.read_text(path)

    def file_size(self, path: str) -> int:
        return int(self.exec(f"wc -c < {shlex.quote(path)}").stdout.strip())

    def wait_for_http(
        self,
        *,
        path: str | None = None,
        timeout: int | None = None,
    ) -> None:
        if not hasattr(self, "http_port"):
            raise AssertionError(f"{self.name} does not expose an http_port")

        deadline = time.time() + (timeout or self.runtime.health_timeout)
        url = f"http://127.0.0.1:{self.http_port}{path or self.runtime.health_path}"

        while time.time() < deadline:
            if not self.is_running():
                raise AssertionError(
                    f"{self.name} stopped before HTTP became healthy.\nLogs:\n{self.logs()}"
                )

            result = run_command(["curl", "-fsS", url], check=False)
            if result.returncode == 0:
                return
            time.sleep(2)

        raise AssertionError(
            f"{self.name} did not become healthy.\nLogs:\n{self.logs()}"
        )

    def wait_for_tcp(
        self,
        port_attr: str,
        *,
        timeout: int = 120,
        interval: int = 2,
    ) -> None:
        port = getattr(self, port_attr)
        deadline = time.time() + timeout

        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(interval)

        raise AssertionError(
            f"{self.name} did not expose {port_attr}.\nLogs:\n{self.logs()}"
        )

    def wait_for_smtp(self, *, timeout: int = 120) -> None:
        self.wait_for_tcp("smtp_port", timeout=timeout)

    def wait_for_log(self, needle: str, *, timeout: int = 90) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.logs():
                return
            time.sleep(2)

        raise AssertionError(
            f"{needle!r} not found in logs for {self.name}.\nLogs:\n{self.logs()}"
        )

    def wait_for_exit(self, *, timeout: int = 45) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.runtime.inspect_state(self.name, "State.Status")
            if status == "exited":
                return status
            time.sleep(1)

        raise AssertionError(f"{self.name} did not exit in time.\nLogs:\n{self.logs()}")


def pytest_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _format_env_value(value: str, attrs: Mapping[str, object]) -> str:
    if "{" not in value:
        return value
    context = {key: str(attr_value) for key, attr_value in attrs.items()}
    return value.format_map(context)
