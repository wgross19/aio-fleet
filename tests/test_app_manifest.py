from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aio_fleet.app_manifest import (
    APP_MANIFEST_NAME,
    load_app_manifest,
    render_app_manifest,
    validate_app_manifest,
)
from aio_fleet.manifest import ManifestError, load_manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fleet.yml"


def test_render_app_manifest_exports_sure_control_plane_surface() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    content = render_app_manifest(repo)
    rendered = yaml.safe_load(content)

    assert rendered["schema_version"] == 1  # nosec B101
    assert rendered["repo"] == "sure-aio"  # nosec B101
    assert rendered["image"]["name"] == "wgross19/sure-aio"  # nosec B101
    assert rendered["release"]["profile"] == "upstream-aio-track"  # nosec B101
    assert rendered["template"]["xml_paths"] == [  # nosec B101
        "sure-aio.xml",
        "sure-aio-alpha.xml",
    ]
    assert rendered["catalog"]["assets"] == [  # nosec B101
        {"source": "sure-aio.xml", "target": "sure-aio.xml"},
        {"source": "sure-aio-alpha.xml", "target": "sure-aio-alpha.xml"},
        {
            "source": "screenshots/sure-aio/01-dashboard.png",
            "target": "screenshots/sure-aio/01-dashboard.png",
        },
        {
            "source": "screenshots/sure-aio/02-account-activity.png",
            "target": "screenshots/sure-aio/02-account-activity.png",
        },
        {
            "source": "screenshots/sure-aio/03-budgets.png",
            "target": "screenshots/sure-aio/03-budgets.png",
        },
        {
            "source": "screenshots/sure-aio-alpha/01-login.png",
            "target": "screenshots/sure-aio-alpha/01-login.png",
        },
        {
            "source": "screenshots/sure-aio-alpha/02-account-activity.png",
            "target": "screenshots/sure-aio-alpha/02-account-activity.png",
        },
        {
            "source": "screenshots/sure-aio-alpha/03-budgets.png",
            "target": "screenshots/sure-aio-alpha/03-budgets.png",
        },
    ]
    assert "  xml_paths:\n    - sure-aio.xml\n" in content  # nosec B101
    assert "    - sure-aio-alpha.xml\n" in content  # nosec B101
    assert rendered["components"]["sure-alpha"]["release_policy"] == (  # nosec B101
        "registry_only"
    )
    assert "    - Productivity\n" in content  # nosec B101


def test_render_app_manifest_quotes_numeric_string_targets() -> None:
    repo = load_manifest(FIXTURE).repo("mem0-aio")

    content = render_app_manifest(repo)

    assert '    - "3000"\n' in content  # nosec B101
    assert '    - "8765"\n' in content  # nosec B101


def test_render_app_manifest_uses_prettier_stable_yaml() -> None:
    dify = render_app_manifest(load_manifest(FIXTURE).repo("dify-aio"))
    signoz = render_app_manifest(load_manifest(FIXTURE).repo("signoz-aio"))

    assert '    - "*.xml"\n' in dify  # nosec B101
    assert (  # nosec B101
        "image_description: Unraid-friendly SigNoz OpenTelemetry collector companion for remote and local hosts\n"
        in signoz
    )


def test_load_app_manifest_validates_required_sections(tmp_path: Path) -> None:
    path = tmp_path / APP_MANIFEST_NAME
    path.write_text("""
schema_version: 1
repo: example-aio
github_repo: wgross19/example-aio
app_slug: example-aio
image:
  name: wgross19/example-aio
  cache_scope: example-aio-image
  pytest_tag: example-aio:pytest
release:
  name: Example AIO
  profile: changelog-version
""")

    manifest = load_app_manifest(path)

    assert manifest["repo"] == "example-aio"  # nosec B101


def test_validate_app_manifest_rejects_missing_image_tag() -> None:
    with pytest.raises(ManifestError, match="image missing required key: pytest_tag"):
        validate_app_manifest(
            {
                "schema_version": 1,
                "repo": "example-aio",
                "github_repo": "wgross19/example-aio",
                "app_slug": "example-aio",
                "image": {
                    "name": "wgross19/example-aio",
                    "cache_scope": "example-aio-image",
                },
                "release": {"name": "Example AIO", "profile": "changelog-version"},
            }
        )
