from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from xml.etree.ElementTree import ParseError  # nosec B405

import defusedxml.ElementTree as ET

SECRET_KEYWORDS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)

REQUIRED_METADATA_FIELDS = (
    "Name",
    "Repository",
    "Support",
    "Project",
    "TemplateURL",
    "Icon",
    "Category",
    "WebUI",
)


@dataclass(frozen=True)
class ContainerContract:
    image: str
    template_xml: Path
    dockerfile: Path | None = None
    ports: tuple[str, ...] = ()
    persistent_paths: tuple[str, ...] = ()
    health_url: str = ""
    expected_labels: dict[str, str] = field(default_factory=dict)
    secret_files: tuple[Path, ...] = ()
    expected_privileged: str | None = "false"
    required_metadata_fields: tuple[str, ...] = REQUIRED_METADATA_FIELDS

    @property
    def effective_dockerfile(self) -> Path:
        return self.dockerfile or self.template_xml.parent / "Dockerfile"


def assert_template_declares_contract(contract: ContainerContract) -> None:
    targets = _template_targets(contract.template_xml)
    missing_ports = sorted(set(contract.ports) - targets)
    missing_paths = sorted(set(contract.persistent_paths) - targets)
    assert (
        not missing_ports
    ), f"template missing port target(s): {missing_ports}"  # nosec B101
    assert (
        not missing_paths
    ), f"template missing path target(s): {missing_paths}"  # nosec B101


def assert_unraid_metadata_contract(contract: ContainerContract) -> None:
    root = _template_root(contract.template_xml)
    if contract.expected_privileged is not None:
        assert root.findtext("Privileged") == contract.expected_privileged  # nosec B101
    for tag in contract.required_metadata_fields:
        value = root.findtext(tag)
        assert value and value.strip(), f"{tag} must be populated"  # nosec B101
    assert _config_elements(
        contract.template_xml
    ), "template must expose configurable settings"  # nosec B101


def assert_secret_like_template_variables_are_masked(template_xml: Path) -> None:
    for config in _config_elements(template_xml):
        name = config.get("Name") or ""
        target = config.get("Target") or ""
        default = config.get("Default") or ""
        if (
            target.endswith("_PATH")
            or target.endswith("_ENABLED")
            or target.startswith(("MAX_", "MIN_"))
            or name.upper().endswith(" PATH")
            or set(default.split("|")) == {"false", "true"}
        ):
            continue
        haystack = " ".join(filter(None, (name, target))).upper()
        if any(keyword in haystack for keyword in SECRET_KEYWORDS):
            assert (
                config.get("Mask") == "true"
            ), (  # nosec B101
                f"{config.get('Name') or config.get('Target')} should be masked"
            )


def assert_required_appdata_paths_declared_as_volumes(
    contract: ContainerContract,
) -> None:
    volumes = _dockerfile_volumes(contract.effective_dockerfile)
    assert volumes, "Dockerfile must declare persistent volumes"  # nosec B101

    for config in _config_elements(contract.template_xml):
        if config.get("Type") != "Path" or config.get("Required") != "true":
            continue
        default = config.get("Default") or config.text or ""
        target = config.get("Target") or ""
        if not default.startswith("/mnt/user/appdata"):
            continue
        assert any(
            target == volume or target.startswith(f"{volume.rstrip('/')}/")
            for volume in volumes
        ), f"{target} must be covered by a Dockerfile VOLUME"  # nosec B101


def assert_template_ports_exposed_by_image(contract: ContainerContract) -> None:
    exposed_ports = _dockerfile_exposed_ports(contract.effective_dockerfile)
    assert exposed_ports, "Dockerfile must expose template ports"  # nosec B101

    for config in _config_elements(contract.template_xml):
        if config.get("Type") == "Port":
            assert config.get("Target") in exposed_ports  # nosec B101


def assert_dockerfile_runtime_safety_contract(contract: ContainerContract) -> None:
    dockerfile = _dockerfile_text(contract.effective_dockerfile)
    arg_defaults = _dockerfile_arg_defaults(contract.effective_dockerfile)
    from_lines = []
    for line in dockerfile.splitlines():
        if not line.startswith("FROM "):
            continue
        parts = line.split()
        if len(parts) > 1:
            from_lines.append(parts[1])

    assert from_lines, "Dockerfile must declare at least one base image"  # nosec B101
    for image in from_lines:
        digest_arg = re.search(r"@\$\{([^}]+)\}", image)
        assert "@sha256:" in image or (  # nosec B101
            digest_arg
            and arg_defaults.get(digest_arg.group(1), "").startswith("sha256:")
        ), f"{image} must be digest-pinned"

    assert "HEALTHCHECK" in dockerfile  # nosec B101
    assert "curl -fsS" in dockerfile  # nosec B101
    assert 'ENTRYPOINT ["/init"]' in dockerfile  # nosec B101
    assert "S6_CMD_WAIT_FOR_SERVICES_MAXTIME" in dockerfile  # nosec B101
    assert "S6_BEHAVIOUR_IF_STAGE2_FAILS=2" in dockerfile  # nosec B101


def assert_docker_socket_mount_is_advanced_when_present(template_xml: Path) -> None:
    for config in _config_elements(template_xml):
        if config.get("Target") != "/var/run/docker.sock":
            continue
        description = (config.get("Description") or "").lower()
        assert config.get("Display") == "advanced"  # nosec B101
        assert config.get("Required") == "false"  # nosec B101
        assert "socket" in description and "security" in description  # nosec B101


def assert_image_labels(image: str, expected: dict[str, str]) -> None:
    if not expected:
        return
    data = docker_inspect_image(image)
    labels = data.get("Config", {}).get("Labels", {}) or {}
    missing = {
        key: value for key, value in expected.items() if labels.get(key) != value
    }
    assert (
        not missing
    ), f"image labels did not match expected values: {missing}"  # nosec B101


def assert_owner_only_files(paths: tuple[Path, ...]) -> None:
    for path in paths:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert (
            mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
        ), (  # nosec B101
            f"{path} must not be readable, writable, or executable by group/other"
        )


def assert_http_ready(url: str, *, timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=3) as response:  # nosec B310
                if 200 <= response.status < 500:
                    return
        except URLError as exc:
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"{url} did not become ready: {last_error}")


def docker_inspect_image(image: str) -> dict[str, object]:
    docker = shutil.which("docker")
    assert docker, "docker is required to inspect container images"  # nosec B101
    result = subprocess.run(  # nosec B603
        [docker, "image", "inspect", image],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr  # nosec B101
    inspected = json.loads(result.stdout)
    assert inspected, f"docker image not found: {image}"  # nosec B101
    return inspected[0]


def should_use_prebuilt_image() -> bool:
    return os.environ.get("AIO_PYTEST_USE_PREBUILT_IMAGE", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _template_targets(template_xml: Path) -> set[str]:
    try:
        root = ET.parse(template_xml).getroot()
    except ParseError as exc:
        raise AssertionError(
            f"unable to parse template XML {template_xml}: {exc}"
        ) from exc
    targets = set()
    for config in root.findall(".//Config"):
        target = config.attrib.get("Target", "").strip()
        if target:
            targets.add(target)
    return targets


def _template_root(template_xml: Path) -> ET.Element:
    try:
        return ET.parse(template_xml).getroot()
    except ParseError as exc:
        raise AssertionError(
            f"unable to parse template XML {template_xml}: {exc}"
        ) from exc


def _config_elements(template_xml: Path) -> list[ET.Element]:
    return list(_template_root(template_xml).findall("Config"))


def _dockerfile_text(dockerfile: Path) -> str:
    return dockerfile.read_text()


def _dockerfile_volumes(dockerfile: Path) -> set[str]:
    volumes: set[str] = set()
    for match in re.finditer(
        r"(?m)^VOLUME\s+(\[[^\]]+\])", _dockerfile_text(dockerfile)
    ):
        volumes.update(json.loads(match.group(1)))
    return volumes


def _dockerfile_exposed_ports(dockerfile: Path) -> set[str]:
    ports: set[str] = set()
    for line in _dockerfile_text(dockerfile).splitlines():
        if not line.startswith("EXPOSE "):
            continue
        for token in line.split()[1:]:
            ports.add(token.split("/", 1)[0])
    return ports


def _dockerfile_arg_defaults(dockerfile: Path) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in _dockerfile_text(dockerfile).splitlines():
        if not line.startswith("ARG ") or "=" not in line:
            continue
        name, value = line.removeprefix("ARG ").split("=", 1)
        defaults[name] = value
    return defaults
