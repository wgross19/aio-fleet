from __future__ import annotations

from pathlib import Path

from aio_fleet.manifest import RepoConfig, load_manifest
from aio_fleet.validators import (
    catalog_quality_findings,
    catalog_repo_failures,
    derived_repo_failures,
    pinned_action_failures,
    publish_platform_failures,
    release_hardening_failures,
    repo_local_workflow_failures,
    runtime_contract_failures,
    template_metadata_failures,
    tracked_artifact_failures,
)


class _Manifest:
    raw = {"awesome_unraid_repository": "wgross19/awesome-unraid"}
    repos: dict[str, RepoConfig] = {}


def _repo(tmp_path: Path, name: str = "example-aio", **overrides: object) -> RepoConfig:
    raw = {
        "path": str(tmp_path),
        "app_slug": name,
        "image_name": f"wgross19/{name}",
        "docker_cache_scope": f"{name}-image",
        "pytest_image_tag": f"{name}:pytest",
        "publish_profile": "changelog-version",
        "publish_platforms": "linux/amd64,linux/arm64",
        "public": True,
        "catalog_assets": [{"source": f"{name}.xml", "target": f"{name}.xml"}],
    }
    raw.update(overrides)
    return RepoConfig(name=name, raw=raw, defaults={}, owner="wgross19")


def _write_minimal_derived_repo(tmp_path: Path) -> None:
    for path in [
        "Dockerfile",
        "README.md",
        "pyproject.toml",
        ".aio-fleet.yml",
        "tests/integration/test_container_runtime.py",
        "example-aio.xml",
    ]:
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("ok\n")


def _write_catalog_readme(
    catalog_path: Path,
    *,
    available: list[str] | None = None,
    in_progress: list[str] | None = None,
    candidates: list[str] | None = None,
    available_count: int | None = None,
    image_names: list[str] | None = None,
    image_count: int | None = None,
    star_repos: list[str] | None = None,
    star_type: str = "Date",
) -> None:
    available = ["example-aio"] if available is None else available
    in_progress = [] if in_progress is None else in_progress
    candidates = [] if candidates is None else candidates
    count = len(available) if available_count is None else available_count
    image_names = ["wgross19/example-aio"] if image_names is None else image_names
    image_count = len(image_names) if image_count is None else image_count
    star_repos = star_repos or ["wgross19/awesome-unraid", "wgross19/example-aio"]
    image_query = f"repos={','.join(star_repos)}&type={star_type}&theme=dark"
    link_fragment = "&".join(star_repos + ["Date"])
    lines = [
        "# Awesome Unraid",
        "",
        f"- Available templates: `{count}`",
        f"- Published image packages: `{image_count}`",
        "",
        f"### Available Templates ({count})",
        "",
        *[
            f"- **[{name}](https://github.com/wgross19/{name})** - Ready."
            for name in available
        ],
        "",
        f"### In Progress ({len(in_progress)})",
        "",
        *[
            f"- **[{name}](https://github.com/wgross19/{name})** - In progress."
            for name in in_progress
        ],
        "",
        "### Upcoming Candidates",
        "",
        *[f"- **{name}** - Candidate." for name in candidates],
        "",
        "## Published Image Packages",
        "",
        "| Image | Source | Notes |",
        "| --- | --- | --- |",
        *[
            "| "
            f"[`{image}`](https://hub.docker.com/r/{image}) "
            "| [`wgross19/example-aio`](https://github.com/wgross19/example-aio) "
            "| `example-aio` |"
            for image in image_names
        ],
        "",
        "## Star History",
        "",
        (
            "[![Star History Chart]"
            f"(https://api.star-history.com/svg?{image_query})]"
            f"(https://star-history.com/#{link_fragment})"
        ),
        "",
    ]
    (catalog_path / "README.md").write_text("\n".join(lines))


def test_pinned_action_validation_rejects_tagged_actions(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "build.yml").write_text("""
jobs:
  test:
    steps:
      - uses: actions/checkout@v6
""")

    assert pinned_action_failures(tmp_path) == [  # nosec B101
        ".github/workflows/build.yml: action is not pinned to a full SHA -> actions/checkout@v6"
    ]


def test_pinned_action_validation_covers_yaml_and_action_yaml(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    action_dir = tmp_path / ".github" / "actions" / "local"
    workflow_dir.mkdir(parents=True)
    action_dir.mkdir(parents=True)
    (workflow_dir / "build.yaml").write_text("""
jobs:
  test:
    steps:
      - uses: actions/checkout@v6
""")
    (action_dir / "action.yaml").write_text("""
runs:
  using: composite
  steps:
    - uses: actions/setup-python@v6
""")

    assert pinned_action_failures(tmp_path) == [  # nosec B101
        ".github/actions/local/action.yaml: action is not pinned to a full SHA -> actions/setup-python@v6",
        ".github/workflows/build.yaml: action is not pinned to a full SHA -> actions/checkout@v6",
    ]


def test_app_repo_local_workflows_are_rejected(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "pwn.yml").write_text("name: pwn\n")

    assert repo_local_workflow_failures(_repo(tmp_path)) == [  # nosec B101
        "example-aio: .github/workflows is disabled; app repos are checked by aio-fleet / required"
    ]


def test_release_hardening_rejects_floating_bundle_updates(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "RUN bundle config set frozen false && bundle update --conservative rack\n"
    )

    failures = release_hardening_failures(_repo(tmp_path))

    assert (  # nosec B101
        "example-aio: Dockerfile must not run floating Bundler updates during image builds: bundle update"
        in failures
    )
    assert (  # nosec B101
        "example-aio: Dockerfile must not disable Bundler frozen mode during image builds: bundle config set frozen false"
        in failures
    )


def test_release_hardening_accepts_mem0_apt_and_openmemory_policy(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "\n".join(
            [
                "FROM ubuntu:26.04 AS runtime-base",
                "COPY docker/normalize-apt-sources.sh /usr/local/bin/normalize-apt-sources",
                "RUN /usr/local/bin/normalize-apt-sources && apt-get update",
                "FROM runtime-base AS runtime",
            ]
        )
    )
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker/normalize-apt-sources.sh").write_text(
        "\n".join(
            [
                "http://archive\\.ubuntu\\.com/ubuntu/",
                "http://security\\.ubuntu\\.com/ubuntu/",
                "http://ports\\.ubuntu\\.com/ubuntu-ports/",
                "plaintext apt source remained",
                "insecure apt source option is not allowed",
            ]
        )
    )
    (tmp_path / ".gitmodules").write_text("url = https://github.com/mem0ai/mem0\n")
    (tmp_path / ".aio-fleet.yml").write_text("repo: mem0-aio\n")

    failures = release_hardening_failures(_repo(tmp_path, name="mem0-aio"))

    assert failures == []  # nosec B101


def test_release_hardening_rejects_mem0_mutable_fork_policy(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu:26.04\n"
        'RUN case "$uri" in http://archive.ubuntu.com/ubuntu/|https://archive.ubuntu.com/ubuntu/) ;; esac && apt-get update\n'
    )
    (tmp_path / ".gitmodules").write_text("url = https://github.com/wgross19/mem0\n")
    (tmp_path / ".aio-fleet.yml").write_text(
        "submodule_ref_template: codex/openmemory-{version}-aio\n"
    )

    failures = release_hardening_failures(_repo(tmp_path, name="mem0-aio"))

    assert (  # nosec B101
        "mem0-aio: Dockerfile must expose a runtime-base APT validation stage"
        in failures
    )
    assert (  # nosec B101
        "mem0-aio: openmemory submodule must not use the wgross19/mem0 fork"
        in failures
    )
    assert (  # nosec B101
        "mem0-aio: .aio-fleet.yml must not target mutable OpenMemory fork branch templates"
        in failures
    )


def _write_sure_hardening_fixture(
    tmp_path: Path,
    *,
    proxy_mask: str = "true",
    redis_url_mask: str = "true",
    session_default: str = "",
) -> None:
    for path in ("Dockerfile", "Dockerfile.alpha"):
        (tmp_path / path).write_text("RUN bundle check\n")
    for path in ("sure-aio.xml", "sure-aio-alpha.xml"):
        (tmp_path / path).write_text(f"""<?xml version="1.0"?>
<Container version="2">
  <Config Name="HTTP Proxy" Target="HTTP_PROXY" Mask="{proxy_mask}"/>
  <Config Name="HTTPS Proxy" Target="HTTPS_PROXY" Mask="{proxy_mask}"/>
  <Config Name="No Proxy" Target="NO_PROXY" Mask="false"/>
  <Config Name="Redis URL" Target="REDIS_URL" Mask="{redis_url_mask}"/>
  <Config Name="Session Key" Target="EXTERNAL_ASSISTANT_SESSION_KEY" Default="{session_default}" Description="isolated per-chat remote state">{session_default}</Config>
</Container>
""")
    session = (
        tmp_path
        / "rootfs/rails/config/initializers/sure_aio_external_assistant_session_key.rb"
    )
    session.parent.mkdir(parents=True)
    session.write_text(
        "SureAioExternalAssistantSessionKey\n"
        'ENV["EXTERNAL_ASSISTANT_SESSION_KEY"].to_s.strip\n'
        "sure-chat:\n"
        "chat&.id\n"
    )
    web_dep = tmp_path / "rootfs/etc/s6-overlay/s6-rc.d/web/dependencies.d/postgres"
    web_dep.parent.mkdir(parents=True)
    web_dep.write_text("")
    web_run = tmp_path / "rootfs/etc/s6-overlay/s6-rc.d/web/run"
    web_run.write_text('PGPASSWORD="${POSTGRES_PASSWORD}" psql -d "${POSTGRES_DB}"\n')


def test_release_hardening_accepts_sure_stable_and_alpha_policy(
    tmp_path: Path,
) -> None:
    _write_sure_hardening_fixture(tmp_path)
    repo = _repo(
        tmp_path,
        name="sure-aio",
        catalog_assets=[
            {"source": "sure-aio.xml", "target": "sure-aio.xml"},
            {"source": "sure-aio-alpha.xml", "target": "sure-aio-alpha.xml"},
        ],
        components={
            "aio": {"dockerfile": "Dockerfile", "xml_paths": ["sure-aio.xml"]},
            "sure-alpha": {
                "dockerfile": "Dockerfile.alpha",
                "xml_paths": ["sure-aio-alpha.xml"],
            },
        },
    )

    failures = release_hardening_failures(repo)

    assert failures == []  # nosec B101


def test_release_hardening_rejects_sure_secret_and_component_drift(
    tmp_path: Path,
) -> None:
    _write_sure_hardening_fixture(
        tmp_path,
        proxy_mask="false",
        redis_url_mask="false",
        session_default="agent:main:main",
    )
    repo = _repo(
        tmp_path,
        name="sure-aio",
        catalog_assets=[
            {"source": "sure-aio.xml", "target": "sure-aio.xml"},
            {"source": "sure-aio-alpha.xml", "target": "sure-aio-alpha.xml"},
        ],
        components={"aio": {"dockerfile": "Dockerfile", "xml_paths": ["sure-aio.xml"]}},
    )

    failures = release_hardening_failures(repo)

    assert (
        "sure-aio: components must include sure-alpha package" in failures
    )  # nosec B101
    assert (  # nosec B101
        "sure-aio: sure-aio.xml Config Target HTTP_PROXY must be masked" in failures
    )
    assert (  # nosec B101
        "sure-aio: sure-aio-alpha.xml Config Target HTTPS_PROXY must be masked"
        in failures
    )
    assert (  # nosec B101
        "sure-aio: sure-aio.xml Config Target REDIS_URL must be masked" in failures
    )
    assert (  # nosec B101
        "sure-aio: sure-aio.xml EXTERNAL_ASSISTANT_SESSION_KEY default must be blank"
        in failures
    )


def test_derived_repo_validation_accepts_minimal_repo(tmp_path: Path) -> None:
    _write_minimal_derived_repo(tmp_path)

    assert derived_repo_failures(tmp_path) == []  # nosec B101


def test_derived_repo_validation_rejects_template_leftovers(tmp_path: Path) -> None:
    repo_path = tmp_path / "example-aio"
    repo_path.mkdir()
    _write_minimal_derived_repo(repo_path)
    (repo_path / "template-aio.xml").write_text("leftover\n")

    assert derived_repo_failures(repo_path) == [  # nosec B101
        "remove template placeholder path in derived repo: template-aio.xml"
    ]


def test_derived_repo_validation_loads_component_templates(tmp_path: Path) -> None:
    _write_minimal_derived_repo(tmp_path)
    (tmp_path / "components.toml").write_text("""
[components.agent]
template = "agent.xml"
""")

    assert derived_repo_failures(tmp_path) == [
        "missing required file: agent.xml"
    ]  # nosec B101


def test_derived_repo_validation_rejects_escaped_component_template(
    tmp_path: Path,
) -> None:
    _write_minimal_derived_repo(tmp_path)
    (tmp_path / "components.toml").write_text("""
[components.agent]
template = "../agent.xml"
""")

    assert derived_repo_failures(tmp_path) == [
        "repo path escapes checkout: ../agent.xml"
    ]  # nosec B101


def test_publish_platform_validation_rejects_unhandled_arm64(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("""
ARG TARGETARCH
RUN case "${TARGETARCH}" in amd64) echo ok ;; *) exit 1 ;; esac
""")

    failures = publish_platform_failures(_repo(tmp_path))

    assert failures == [  # nosec B101
        "example-aio: Dockerfile does not appear to handle arm64 but publish_platforms includes linux/arm64"
    ]


def test_template_metadata_validation_checks_catalog_urls(tmp_path: Path) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>ghcr.io/wgross19/example-aio:latest</Repository>
  <Registry>https://ghcr.io/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/wrong.xml</TemplateURL>
  <Icon>https://example.com/icon.png</Icon>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (  # nosec B101
        "example-aio: example-aio.xml TemplateURL must be "
        "https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml, got "
        "https://raw.githubusercontent.com/wgross19/awesome-unraid/main/wrong.xml"
    ) in failures
    assert (  # nosec B101
        "example-aio: example-aio.xml Icon must point at wgross19/awesome-unraid/main/icons/"
    ) in failures
    assert (  # nosec B101
        "example-aio: example-aio.xml <Repository> must use Docker Hub shorthand, got ghcr.io/wgross19/example-aio:latest"
        in failures
    )
    assert (  # nosec B101
        "example-aio: example-aio.xml <Registry> must point at Docker Hub, got https://ghcr.io/wgross19/example-aio"
        in failures
    )


def test_template_metadata_validation_rejects_untrusted_repository_namespace(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>attacker/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/attacker/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (  # nosec B101
        "example-aio: example-aio.xml <Repository> must use one of ['wgross19/example-aio'], got attacker/example-aio:latest"
        in failures
    )


def test_template_metadata_validation_handles_non_file_xml(tmp_path: Path) -> None:
    (tmp_path / "example-aio.xml").mkdir()

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert failures == [  # nosec B101
        "example-aio: catalog XML example-aio.xml must be a file"
    ]


def test_template_metadata_validation_rejects_defused_xml(tmp_path: Path) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<Container version="2"><Name>&xxe;</Name></Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert len(failures) == 1  # nosec B101
    assert failures[0].startswith(  # nosec B101
        "example-aio: unable to parse catalog XML example-aio.xml:"
    )


def test_template_metadata_validation_rejects_nested_options_and_bad_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Changes>bad heading</Changes>
  <Config Name="Mode" Target="MODE"><Option>one</Option></Config>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (  # nosec B101
        "example-aio: example-aio.xml <Changes> must start with '### YYYY-MM-DD'"
        in failures
    )
    assert (  # nosec B101
        "example-aio: example-aio.xml Config Mode uses nested <Option> tags; use pipe-delimited values instead"
        in failures
    )


def test_template_metadata_validation_allows_literal_pipe_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Changes>### 2026-05-20
- Test.</Changes>
  <Config Name="LDAP query" Target="LDAP_QUERY" Default="(&#124;(uid=:username)(mail=:username))">(&#124;(uid=:username)(mail=:username))</Config>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert not any(
        "LDAP query selected value" in failure for failure in failures
    )  # nosec B101


def test_template_metadata_validation_rejects_bad_pipe_dropdown_selection(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Changes>### 2026-05-20
- Test.</Changes>
  <Config Name="Mode" Target="MODE" Default="info|debug">warn</Config>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (  # nosec B101
        "example-aio: example-aio.xml Config Mode selected value 'warn' is not one of ['info', 'debug']"
        in failures
    )


def test_template_metadata_validation_allows_component_changelog_note(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio-alpha.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio-alpha</Name>
  <Repository>wgross19/example-aio-alpha:latest-alpha</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio-alpha</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example alpha.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio-alpha.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Changes>### 2026-05-18
- Generated from CHANGELOG.alpha.md during release preparation. Do not edit manually.
- Alpha release.</Changes>
</Container>
""")
    repo = _repo(
        tmp_path,
        image_name="wgross19/example-aio",
        catalog_assets=[
            {"source": "example-aio-alpha.xml", "target": "example-aio-alpha.xml"}
        ],
        components={
            "alpha": {
                "image_name": "wgross19/example-aio-alpha",
                "release_changelog": "CHANGELOG.alpha.md",
                "xml_paths": ["example-aio-alpha.xml"],
            }
        },
    )

    failures = template_metadata_failures(repo, _Manifest())  # type: ignore[arg-type]

    assert not any(
        "second line must be" in failure for failure in failures
    )  # nosec B101


def test_template_metadata_validation_applies_manifest_declared_targets(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Changes>### 2026-05-01</Changes>
  <Config Name="Present" Target="PRESENT"/>
</Container>
""")

    failures = template_metadata_failures(
        _repo(
            tmp_path,
            validation={
                "required_targets": ["PRESENT", "MISSING"],
                "forbidden_targets": ["PRESENT"],
            },
        ),
        _Manifest(),  # type: ignore[arg-type]
    )

    assert any("MISSING" in failure for failure in failures)  # nosec B101
    assert any("manifest-forbidden" in failure for failure in failures)  # nosec B101


def test_template_metadata_validation_accepts_component_alpha_contract(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio-alpha.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio-alpha</Name>
  <Repository>wgross19/example-aio-alpha:latest-alpha</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio-alpha</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Testing / Unstable alpha lane with Alpha-only controls, latest-alpha tags, and /mnt/user/appdata/example-aio-alpha storage.</Overview>
  <Category>Tools:Utilities</Category>
  <Beta>True</Beta>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio-alpha.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText/>
  <DonateLink/>
  <Changes>### 2026-05-19
- Generated from CHANGELOG.alpha.md during release preparation. Do not edit manually.
- Alpha contract.</Changes>
  <Config Name="[Alpha] Sure NDJSON Upload Limit MB" Target="SURE_IMPORT_MAX_NDJSON_SIZE_MB" Default="250" Description="Alpha-only upload limit.">250</Config>
  <Config Name="[Alpha] Sure Import Max Rows" Target="SURE_IMPORT_MAX_ROWS" Default="1000000" Description="Alpha-only row limit.">1000000</Config>
  <Config Name="[Alpha Auth] WebAuthn Relying Party ID" Target="WEBAUTHN_RP_ID" Default="" Description="Alpha-only passkey relying party."/>
  <Config Name="[Alpha Auth] WebAuthn Allowed Origins" Target="WEBAUTHN_ALLOWED_ORIGINS" Default="" Description="Alpha-only passkey origins."/>
</Container>
""")

    failures = template_metadata_failures(
        _repo(
            tmp_path,
            catalog_assets=[
                {
                    "source": "example-aio-alpha.xml",
                    "target": "example-aio-alpha.xml",
                }
            ],
            components={
                "alpha": {
                    "image_name": "wgross19/example-aio-alpha",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "xml_paths": ["example-aio-alpha.xml"],
                    "validation": _alpha_validation_contract(),
                }
            },
        ),
        _Manifest(),  # type: ignore[arg-type]
    )

    assert failures == []  # nosec B101


def test_template_metadata_validation_rejects_component_alpha_contract_drift(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio-alpha.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Stable-looking lane with /mnt/user/appdata/example-aio/system storage.</Overview>
  <Category>Tools:Utilities</Category>
  <Beta>False</Beta>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio-alpha.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText/>
  <DonateLink/>
  <Changes>### 2026-05-19
- Generated from CHANGELOG.alpha.md during release preparation. Do not edit manually.
- Alpha contract drift.</Changes>
  <Config Name="Sure NDJSON Upload Limit MB" Target="SURE_IMPORT_MAX_NDJSON_SIZE_MB" Default="100" Description="Upload limit.">100</Config>
  <Config Name="[Alpha Auth] WebAuthn Relying Party ID" Target="WEBAUTHN_RP_ID" Default="" Description="Passkey relying party."/>
  <Config Name="[Alpha Auth] WebAuthn Allowed Origins" Target="WEBAUTHN_ALLOWED_ORIGINS" Default="" Description="Alpha-only passkey origins."/>
</Container>
""")

    failures = template_metadata_failures(
        _repo(
            tmp_path,
            catalog_assets=[
                {
                    "source": "example-aio-alpha.xml",
                    "target": "example-aio-alpha.xml",
                }
            ],
            components={
                "alpha": {
                    "image_name": "wgross19/example-aio-alpha",
                    "release_changelog": "CHANGELOG.alpha.md",
                    "xml_paths": ["example-aio-alpha.xml"],
                    "validation": _alpha_validation_contract(),
                }
            },
        ),
        _Manifest(),  # type: ignore[arg-type]
    )

    assert any("<Beta> must be True" in failure for failure in failures)  # nosec B101
    assert any(
        "missing manifest-required XML text snippet: Testing / Unstable" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "contains manifest-forbidden XML text snippet: <Name>example-aio</Name>"
        in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "missing manifest-required Config Target(s): SURE_IMPORT_MAX_ROWS" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Config Target SURE_IMPORT_MAX_NDJSON_SIZE_MB Name must contain [Alpha]"
        in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Config Target SURE_IMPORT_MAX_NDJSON_SIZE_MB Default must be 250" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Config Target WEBAUTHN_RP_ID Description must contain Alpha-only" in failure
        for failure in failures
    )  # nosec B101


def _alpha_validation_contract() -> dict[str, object]:
    return {
        "required_targets": [
            "SURE_IMPORT_MAX_NDJSON_SIZE_MB",
            "SURE_IMPORT_MAX_ROWS",
            "WEBAUTHN_RP_ID",
            "WEBAUTHN_ALLOWED_ORIGINS",
        ],
        "required_field_values": {
            "Name": "example-aio-alpha",
            "Repository": "wgross19/example-aio-alpha:latest-alpha",
            "Beta": "True",
        },
        "required_text_snippets": [
            "Testing / Unstable",
            "Alpha-only",
            "/mnt/user/appdata/example-aio-alpha",
            "latest-alpha",
        ],
        "forbidden_text_snippets": [
            "<Name>example-aio</Name>",
            "<Repository>wgross19/example-aio:latest</Repository>",
            "/mnt/user/appdata/example-aio/system",
        ],
        "config_target_requirements": {
            "SURE_IMPORT_MAX_NDJSON_SIZE_MB": {
                "name_contains": "[Alpha]",
                "description_contains": "Alpha-only",
                "default": "250",
                "value": "250",
            },
            "SURE_IMPORT_MAX_ROWS": {
                "name_contains": "[Alpha]",
                "description_contains": "Alpha-only",
                "default": "1000000",
                "value": "1000000",
            },
            "WEBAUTHN_RP_ID": {
                "name_contains": "[Alpha Auth]",
                "description_contains": "Alpha-only",
            },
            "WEBAUTHN_ALLOWED_ORIGINS": {
                "name_contains": "[Alpha Auth]",
                "description_contains": "Alpha-only",
            },
        },
    }


def test_runtime_contract_allows_unpublished_optional_port(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        'ENTRYPOINT ["/init"]\n'
        "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000\n"
        "S6_BEHAVIOUR_IF_STAGE2_FAILS=2\n"
        'VOLUME ["/config"]\n'
        "HEALTHCHECK CMD curl -fsS http://localhost:3000/ || exit 1\n"
        "EXPOSE 3000\n"
    )
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Config Name="Web UI Port" Target="3000" Type="Port" Required="true">3000</Config>
  <Config Name="Optional API Port" Target="8765" Type="Port" Required="false" Default=""></Config>
</Container>
""")

    failures = runtime_contract_failures(_repo(tmp_path))

    assert not any(
        "8765 is not exposed" in failure for failure in failures
    )  # nosec B101


def test_runtime_contract_allows_manifest_healthcheck_marker(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        'ENTRYPOINT ["/init"]\n'
        "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000\n"
        "S6_BEHAVIOUR_IF_STAGE2_FAILS=2\n"
        'VOLUME ["/appdata"]\n'
        'HEALTHCHECK CMD pgrep -f "dist/index.js" >/dev/null || exit 1\n'
    )
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <Config Name="Appdata" Target="/appdata" Type="Path" Required="true">/mnt/user/appdata/example-aio</Config>
</Container>
""")

    failures = runtime_contract_failures(
        _repo(
            tmp_path,
            runtime_healthcheck_markers=['pgrep -f "dist/index.js"'],
        )
    )

    assert not any(
        "runtime safety marker" in failure for failure in failures
    )  # nosec B101


def test_template_metadata_validation_rejects_common_quality_drift(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>Example-AIO</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>not-a-url</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example overview with defaults and advanced settings.</Overview>
  <Category>FakeCategory</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText>Support wgross19</DonateText>
  <DonateLink/>
  <Changes>### 2026-01-01</Changes>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (
        "example-aio: example-aio.xml <Name> must be lowercase" in failures
    )  # nosec B101
    assert (
        "example-aio: example-aio.xml <Project> must be an HTTP(S) URL" in failures
    )  # nosec B101
    assert (  # nosec B101
        "example-aio: example-aio.xml <Category> token is not a known Community Apps category: FakeCategory"
        in failures
    )
    assert (  # nosec B101
        "example-aio: example-aio.xml <DonateText> and <DonateLink> must be both blank or both populated"
        in failures
    )


def test_template_metadata_validation_rejects_unknown_ca_subcategory(
    tmp_path: Path,
) -> None:
    (tmp_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example overview with defaults and advanced settings.</Overview>
  <Category>Network:Security Security Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText/>
  <DonateLink/>
  <Changes>### 2026-01-01</Changes>
</Container>
""")

    failures = template_metadata_failures(_repo(tmp_path), _Manifest())  # type: ignore[arg-type]

    assert (  # nosec B101
        "example-aio: example-aio.xml <Category> token is not a known Community Apps category: Network:Security"
        in failures
    )
    assert not any(  # nosec B101
        "Security" in failure and "Network:Security" not in failure
        for failure in failures
    )


def test_tracked_artifact_validation_rejects_tfstate_and_pycache(
    tmp_path: Path,
) -> None:
    import subprocess  # nosec B404

    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True
    )  # nosec
    (tmp_path / "infra" / "github").mkdir(parents=True)
    (tmp_path / "infra" / "github" / "terraform.tfstate").write_text("{}\n")
    (tmp_path / "tests" / "__pycache__").mkdir(parents=True)
    (tmp_path / "tests" / "__pycache__" / "test.pyc").write_bytes(b"cache")
    subprocess.run(  # nosec
        [
            "git",
            "add",
            "-f",
            "infra/github/terraform.tfstate",
            "tests/__pycache__/test.pyc",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    failures = tracked_artifact_failures(tmp_path)

    assert any("terraform.tfstate" in failure for failure in failures)  # nosec B101
    assert any("test.pyc" in failure for failure in failures)  # nosec B101


def test_tracked_artifact_validation_rejects_virtualenv_symlink(
    tmp_path: Path,
) -> None:
    import subprocess  # nosec B404

    subprocess.run(
        ["git", "init"], cwd=tmp_path, check=True, capture_output=True
    )  # nosec
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / ".venv-local").symlink_to(".", target_is_directory=True)
    subprocess.run(  # nosec
        ["git", "add", "-f", ".venv-local", "bin/python"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    failures = tracked_artifact_failures(tmp_path)

    assert any(".venv-local" in failure for failure in failures)  # nosec B101


def test_catalog_validation_skips_unpublished_repos(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", catalog_published=False)
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}

    assert catalog_repo_failures(manifest, tmp_path / "catalog") == [  # type: ignore[arg-type] # nosec B101
        f"catalog path missing: {tmp_path / 'catalog'}"
    ]

    (tmp_path / "catalog").mkdir()
    assert catalog_repo_failures(manifest, tmp_path / "catalog") == []  # type: ignore[arg-type] # nosec B101

    (tmp_path / "catalog" / "example-aio.xml").write_text("<Container />\n")
    assert catalog_repo_failures(manifest, tmp_path / "catalog") == [  # type: ignore[arg-type] # nosec B101
        "example-aio: catalog target exists while catalog_published is false: example-aio.xml"
    ]


def test_catalog_validation_allows_catalog_only_ci_without_source_checkout(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "missing-repo")
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"icon")
    _write_catalog_readme(catalog_path)
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    assert catalog_repo_failures(manifest, catalog_path) == []  # type: ignore[arg-type] # nosec B101


def test_catalog_validation_allows_padded_published_image_rows(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "missing-repo")
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"icon")
    _write_catalog_readme(catalog_path)
    readme = catalog_path / "README.md"
    readme.write_text(readme.read_text().replace("| [`wgross19/", "|    [`wgross19/"))
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    assert catalog_repo_failures(manifest, catalog_path) == []  # type: ignore[arg-type] # nosec B101


def test_catalog_validation_reports_readme_template_drift(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "missing-repo")
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"icon")
    _write_catalog_readme(
        catalog_path,
        available=[],
        candidates=["example-aio"],
        available_count=0,
        star_repos=["wgross19/awesome-unraid", "wgross19/example-aio"],
    )
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    failures = catalog_repo_failures(manifest, catalog_path)  # type: ignore[arg-type]

    assert any(
        "available template count must be 1, got 0" in failure for failure in failures
    )  # nosec B101
    assert any(
        "Available Templates missing published template(s): example-aio" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Upcoming Candidates lists published template(s): example-aio" in failure
        for failure in failures
    )  # nosec B101


def test_catalog_validation_reports_star_history_drift(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "missing-repo")
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"icon")
    _write_catalog_readme(
        catalog_path,
        star_repos=["wgross19/awesome-unraid", "wgross19/nanoclaw-aio"],
        star_type="",
    )
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    failures = catalog_repo_failures(manifest, catalog_path)  # type: ignore[arg-type]

    assert any(
        "Star History image missing repo(s): wgross19/example-aio" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Star History image includes unpublished repo(s): wgross19/nanoclaw-aio"
        in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Star History image URL must set type=Date" in failure for failure in failures
    )  # nosec B101


def test_catalog_validation_reports_published_image_drift(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path / "missing-repo",
        components={
            "helper": {
                "image_name": "wgross19/example-helper",
                "release_policy": "registry_only",
            }
        },
    )
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"icon")
    _write_catalog_readme(
        catalog_path,
        image_names=["wgross19/example-aio", "wgross19/extra-image"],
        image_count=3,
    )
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>example-aio</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Example.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
</Container>
""")

    failures = catalog_repo_failures(manifest, catalog_path)  # type: ignore[arg-type]

    assert any(
        "published image package count must be 2, got 3" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Published Image Packages missing image(s): wgross19/example-helper" in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "Published Image Packages includes unexpected image(s): wgross19/extra-image"
        in failure
        for failure in failures
    )  # nosec B101


def test_catalog_quality_audit_reports_catalog_presentation_drift(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    manifest = _Manifest()
    manifest.repos = {"example-aio": repo}
    catalog_path = tmp_path / "catalog"
    (catalog_path / "icons").mkdir(parents=True)
    (catalog_path / "icons" / "example.png").write_bytes(b"not-png")
    (catalog_path / "example-aio.xml").write_text("""<?xml version="1.0"?>
<Container version="2">
  <Name>Example-AIO</Name>
  <Repository>wgross19/example-aio:latest</Repository>
  <Registry>https://hub.docker.com/r/wgross19/example-aio</Registry>
  <Project>https://github.com/wgross19/example-aio</Project>
  <Support>https://github.com/wgross19/example-aio/issues</Support>
  <Overview>Too short.</Overview>
  <Category>Tools:Utilities</Category>
  <TemplateURL>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/example-aio.xml</TemplateURL>
  <Icon>https://raw.githubusercontent.com/wgross19/awesome-unraid/main/icons/example.png</Icon>
  <DonateText/>
  <DonateLink/>
  <Changes>### 2026-01-01</Changes>
</Container>
""")

    findings = catalog_quality_findings(manifest, catalog_path)  # type: ignore[arg-type]

    assert any(
        "<Name> must be lowercase" in finding for finding in findings
    )  # nosec B101
    assert any(
        "fuller CA-facing setup guidance" in finding for finding in findings
    )  # nosec B101
    assert any("not a valid PNG" in finding for finding in findings)  # nosec B101


def test_runtime_contract_rejects_required_docker_socket_without_manifest_flag(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        'ENTRYPOINT ["/init"]\n'
        "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000\n"
        "S6_BEHAVIOUR_IF_STAGE2_FAILS=2\n"
        'VOLUME ["/appdata"]\n'
        "HEALTHCHECK CMD curl -fsS http://localhost:3000/ || exit 1\n"
    )
    (tmp_path / "example-aio.xml").write_text("""<?xml version=\"1.0\"?>
<Container version=\"2\">
  <Config Name=\"Docker Socket\" Type=\"Path\" Target=\"/var/run/docker.sock\" Display=\"always\" Required=\"true\" Description=\"Docker socket security control access warning\">/var/run/docker.sock</Config>
</Container>
""")

    failures = runtime_contract_failures(_repo(tmp_path))

    assert any(
        "Docker socket mount must be advanced" in failure for failure in failures
    )  # nosec B101
    assert any(
        "Docker socket mount must be optional" in failure for failure in failures
    )  # nosec B101


def test_runtime_contract_rejects_required_docker_socket_with_manifest_flag(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        'ENTRYPOINT ["/init"]\n'
        "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000\n"
        "S6_BEHAVIOUR_IF_STAGE2_FAILS=2\n"
        'VOLUME ["/appdata"]\n'
        "HEALTHCHECK CMD curl -fsS http://localhost:3000/ || exit 1\n"
    )
    (tmp_path / "example-aio.xml").write_text("""<?xml version=\"1.0\"?>
<Container version=\"2\">
  <Config Name=\"Docker Socket\" Type=\"Path\" Target=\"/var/run/docker.sock\" Display=\"always\" Required=\"true\" Description=\"Docker socket security control access warning\">/var/run/docker.sock</Config>
</Container>
""")

    failures = runtime_contract_failures(
        _repo(tmp_path, validation={"docker_socket_required": True})
    )

    assert any(
        "Docker socket mount must be advanced" in failure for failure in failures
    )  # nosec B101
    assert any(
        "Docker socket mount must be optional" in failure for failure in failures
    )  # nosec B101


def test_runtime_contract_rejects_hidden_required_docker_socket_with_manifest_flag(
    tmp_path: Path,
) -> None:
    (tmp_path / "Dockerfile").write_text(
        "FROM ubuntu@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        'ENTRYPOINT ["/init"]\n'
        "S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000\n"
        "S6_BEHAVIOUR_IF_STAGE2_FAILS=2\n"
        'VOLUME ["/appdata"]\n'
        "HEALTHCHECK CMD curl -fsS http://localhost:3000/ || exit 1\n"
    )
    (tmp_path / "example-aio.xml").write_text("""<?xml version=\"1.0\"?>
<Container version=\"2\">
  <Config Name=\"Docker Socket\" Type=\"Path\" Target=\"/var/run/docker.sock\" Display=\"advanced\" Required=\"true\" Description=\"Docker socket security control access warning\">/var/run/docker.sock</Config>
</Container>
""")

    failures = runtime_contract_failures(
        _repo(tmp_path, validation={"docker_socket_required": True})
    )

    assert (
        "example-aio: example-aio.xml Docker socket mount must be optional" in failures
    )  # nosec B101
