from __future__ import annotations

from pathlib import Path

import pytest

from aio_fleet.testing import (
    ContainerContract,
    assert_dockerfile_runtime_safety_contract,
    assert_owner_only_files,
    assert_required_appdata_paths_declared_as_volumes,
    assert_secret_like_template_variables_are_masked,
    assert_template_declares_contract,
    assert_template_ports_exposed_by_image,
    assert_unraid_metadata_contract,
)


def test_template_contract_helper_accepts_declared_targets(tmp_path: Path) -> None:
    template = tmp_path / "example-aio.xml"
    template.write_text("""<?xml version="1.0"?>
<Container version="2">
  <Config Name="Web UI" Target="8080"/>
  <Config Name="AppData" Target="/appdata"/>
</Container>
""")

    assert_template_declares_contract(
        ContainerContract(
            image="example:test",
            template_xml=template,
            ports=("8080",),
            persistent_paths=("/appdata",),
        )
    )


def test_template_contract_helper_rejects_missing_targets(tmp_path: Path) -> None:
    template = tmp_path / "example-aio.xml"
    template.write_text('<Container version="2" />\n')

    with pytest.raises(AssertionError, match="template missing port"):
        assert_template_declares_contract(
            ContainerContract(
                image="example:test",
                template_xml=template,
                ports=("8080",),
            )
        )


def test_owner_only_file_helper_rejects_group_readable_file(tmp_path: Path) -> None:
    secret = tmp_path / "generated.env"
    secret.write_text("SECRET=value\n")
    secret.chmod(0o640)

    with pytest.raises(AssertionError, match="must not be readable"):
        assert_owner_only_files((secret,))


def test_runtime_contract_helpers_cover_template_and_dockerfile(tmp_path: Path) -> None:
    template = tmp_path / "example-aio.xml"
    template.write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Project>https://github.com/wgross19/example-aio</Project>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Category>AI</Category>
  <WebUI>http://[IP]:[PORT:8080]</WebUI>
  <Privileged>false</Privileged>
  <Config Name="Web UI" Type="Port" Target="8080"/>
  <Config Name="AppData" Type="Path" Target="/appdata" Required="true" Default="/mnt/user/appdata/example-aio"/>
  <Config Name="API Key" Type="Variable" Target="API_KEY" Mask="true"/>
</Container>
""")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        """FROM example/app@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
VOLUME ["/appdata"]
EXPOSE 8080
ENV S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2
HEALTHCHECK CMD curl -fsS http://localhost:8080/ || exit 1
ENTRYPOINT ["/init"]
"""
    )
    contract = ContainerContract(
        image="example:test",
        template_xml=template,
        dockerfile=dockerfile,
        ports=("8080",),
        persistent_paths=("/appdata",),
    )

    assert_unraid_metadata_contract(contract)
    assert_secret_like_template_variables_are_masked(template)
    assert_required_appdata_paths_declared_as_volumes(contract)
    assert_template_ports_exposed_by_image(contract)
    assert_dockerfile_runtime_safety_contract(contract)
