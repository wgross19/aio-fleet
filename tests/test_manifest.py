from __future__ import annotations

from pathlib import Path

import pytest

from aio_fleet.manifest import ManifestError, load_manifest

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_loads_current_fleet() -> None:
    manifest = load_manifest(ROOT / "fleet.yml")

    assert manifest.owner == "wgross19"  # nosec B101
    assert set(manifest.repos) == {  # nosec B101
        "unraid-aio-template",
        "sure-aio",
        "simplelogin-aio",
        "khoj-aio",
        "mem0-aio",
        "infisical-aio",
        "dify-aio",
        "signoz-aio",
        "nanoclaw-aio",
        "penpot-aio",
    }


def test_manifest_records_known_fleet_exceptions() -> None:
    manifest = load_manifest(ROOT / "fleet.yml")

    assert manifest.repo("mem0-aio").get("checkout_submodules") is True  # nosec B101
    assert manifest.repo("dify-aio").extended_integration is not None  # nosec B101
    assert manifest.repo("signoz-aio").is_signoz_suite  # nosec B101
    assert manifest.repo("nanoclaw-aio").is_multi_component  # nosec B101
    assert (
        manifest.repo("nanoclaw-aio").publish_profile == "multi-component"
    )  # nosec B101
    assert manifest.repo("nanoclaw-aio").get("runtime_supervisor") == "s6"  # nosec B101
    assert manifest.repo("nanoclaw-aio").list_value("runtime_healthcheck_markers") == [
        'pgrep -f "dist/index.js"'
    ]  # nosec B101
    assert manifest.repo("nanoclaw-aio").raw["validation"][
        "exact_category_tokens"
    ] == [  # nosec B101
        "AI",
        "Productivity",
        "Network:Messenger",
        "Tools:Utilities",
    ]
    assert (  # nosec B101
        manifest.repo("signoz-aio").get("upstream_digest_arg")
        == "UPSTREAM_SIGNOZ_DIGEST"
    )


def test_manifest_rejects_unknown_publish_profiles(tmp_path: Path) -> None:
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text("""
owner: wgross19
repos:
  broken-aio:
    path: /tmp/broken-aio
    public: true
    app_slug: broken-aio
    image_name: wgross19/broken-aio
    docker_cache_scope: broken-aio-image
    pytest_image_tag: broken-aio:pytest
    publish_profile: mystery
""")

    with pytest.raises(ManifestError, match="unsupported publish_profile"):
        load_manifest(manifest_path)


def test_manifest_requires_components_for_multi_component_profile(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text("""
owner: wgross19
repos:
  broken-aio:
    path: /tmp/broken-aio
    public: true
    app_slug: broken-aio
    image_name: wgross19/broken-aio
    docker_cache_scope: broken-aio-image
    pytest_image_tag: broken-aio:pytest
    publish_profile: multi-component
""")

    with pytest.raises(
        ManifestError, match="multi-component profile requires components"
    ):
        load_manifest(manifest_path)


def test_manifest_requires_explicit_public_flag(tmp_path: Path) -> None:
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text("""
owner: wgross19
repos:
  broken-aio:
    path: /tmp/broken-aio
    app_slug: broken-aio
    image_name: wgross19/broken-aio
    docker_cache_scope: broken-aio-image
    pytest_image_tag: broken-aio:pytest
""")

    with pytest.raises(ManifestError, match="missing required key: public"):
        load_manifest(manifest_path)


def test_manifest_validates_registry_only_component_publish_shape(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "fleet.yml"
    manifest_path.write_text("""
owner: wgross19
repos:
  broken-aio:
    path: /tmp/broken-aio
    public: true
    app_slug: broken-aio
    image_name: wgross19/broken-aio
    docker_cache_scope: broken-aio-image
    pytest_image_tag: broken-aio:pytest
    publish_profile: multi-component
    components:
      aio:
        dockerfile: Dockerfile
      helper:
        image_name: wgross19/broken-helper
        release_policy: registry_only
""")

    with pytest.raises(
        ManifestError,
        match="component helper missing required key: dockerfile",
    ):
        load_manifest(manifest_path)
