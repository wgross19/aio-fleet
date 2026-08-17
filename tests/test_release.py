from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path

from aio_fleet.release import (
    find_release_publish_target_commit,
    latest_aio_release_tag,
    latest_changelog_version,
    latest_component_changelog_version,
    latest_component_release_tag,
    main,
    next_aio_release_version,
    next_semver_release_version,
    read_upstream_version,
    release_subject_matches,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # nosec


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.check_output(  # nosec B603 B607
        ["git", *args], cwd=repo, text=True
    ).strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "config", "commit.gpgsign", "false")


def test_release_helpers_read_declarative_upstream_version(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    upstream = tmp_path / "upstream.toml"
    dockerfile.write_text("ARG UPSTREAM_APP_VERSION=v1.2.3@sha256:abc\n")
    upstream.write_text('[upstream]\nversion_key = "UPSTREAM_APP_VERSION"\n')

    assert read_upstream_version(dockerfile, upstream) == "v1.2.3"  # nosec B101


def test_aio_next_version_uses_upstream_version_and_revision_tags(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.0\n")
    (tmp_path / "upstream.toml").write_text("[upstream]\n")
    _commit(tmp_path, "feat(test): initial")
    _git(tmp_path, "tag", "v2.0.0-aio.1")

    assert (  # nosec B101
        next_aio_release_version(
            tmp_path, tmp_path / "Dockerfile", tmp_path / "upstream.toml"
        )
        == "v2.0.0-aio.2"
    )


def test_aio_next_version_supports_prefixed_component_tags(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile.alpha").write_text("ARG UPSTREAM_VERSION=0.7.1-alpha.9\n")
    (tmp_path / "upstream.toml").write_text("[upstream]\n")
    _commit(tmp_path, "feat(test): initial")
    _git(tmp_path, "tag", "sure-alpha/0.7.1-alpha.9-aio.1")

    assert (  # nosec B101
        next_aio_release_version(
            tmp_path,
            tmp_path / "Dockerfile.alpha",
            tmp_path / "upstream.toml",
            tag_prefix="sure-alpha/",
        )
        == "0.7.1-alpha.9-aio.2"
    )


def test_aio_next_version_preserves_existing_v_prefix_for_unprefixed_upstream(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text("ARG PENPOT_VERSION=2.15.3\n")
    (tmp_path / "upstream.toml").write_text(
        '[upstream]\nversion_key = "PENPOT_VERSION"\n'
    )
    _commit(tmp_path, "feat(test): initial")
    _git(tmp_path, "tag", "v2.15.3-aio.1")

    assert (  # nosec B101
        latest_aio_release_tag(
            tmp_path,
            tmp_path / "Dockerfile",
            tmp_path / "upstream.toml",
            version_key="PENPOT_VERSION",
        )
        == "v2.15.3-aio.1"
    )
    assert (  # nosec B101
        next_aio_release_version(
            tmp_path,
            tmp_path / "Dockerfile",
            tmp_path / "upstream.toml",
            version_key="PENPOT_VERSION",
        )
        == "v2.15.3-aio.2"
    )


def test_aio_next_version_keeps_unprefixed_release_series(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.14.2\n")
    (tmp_path / "upstream.toml").write_text("[upstream]\n")
    _commit(tmp_path, "feat(test): initial")
    _git(tmp_path, "tag", "1.14.2-aio.2")

    assert (  # nosec B101
        next_aio_release_version(
            tmp_path, tmp_path / "Dockerfile", tmp_path / "upstream.toml"
        )
        == "1.14.2-aio.3"
    )


def test_latest_component_release_tag_ignores_namespaced_alpha_tags(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("initial\n")
    _commit(tmp_path, "feat(test): initial")
    _git(tmp_path, "tag", "0.7.0-aio.1")
    (tmp_path / "README.md").write_text("alpha\n")
    _commit(tmp_path, "chore(test): alpha")
    _git(tmp_path, "tag", "sure-alpha/0.7.1-alpha.7-aio.1")

    assert latest_component_release_tag(tmp_path, "aio") == "0.7.0-aio.1"  # nosec B101


def test_semver_next_version_uses_conventional_commit_bump(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("initial\n")
    _commit(tmp_path, "chore(test): initial")
    _git(tmp_path, "tag", "v1.2.3")
    (tmp_path / "README.md").write_text("feature\n")
    _commit(tmp_path, "feat(test): add feature")

    assert next_semver_release_version(tmp_path) == "v1.3.0"  # nosec B101


def test_latest_changelog_version_supports_linked_headings(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [v1.2.3-aio.1](https://example.invalid)\n\n- test\n")

    assert latest_changelog_version(changelog) == "v1.2.3-aio.1"  # nosec B101


def test_latest_component_changelog_version_uses_matching_suffix(
    tmp_path: Path,
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join(
            [
                "# Changelog",
                "",
                "## 0.152.0-agent.1 - 2026-05-17",
                "",
                "- agent",
                "",
                "## v0.124.0-aio.1 - 2026-05-17",
                "",
                "- aio",
            ]
        )
    )

    assert (  # nosec B101
        latest_component_changelog_version(
            changelog, upstream_version="v0.124.0", suffix="aio"
        )
        == "v0.124.0-aio.1"
    )
    assert (  # nosec B101
        latest_component_changelog_version(
            changelog, upstream_version="0.152.0", suffix="agent"
        )
        == "0.152.0-agent.1"
    )


def test_latest_component_changelog_version_accepts_existing_v_prefix(
    tmp_path: Path,
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "\n".join(
            [
                "# Changelog",
                "",
                "## v2.15.3-aio.2 - 2026-05-21",
                "",
                "- template fix",
            ]
        )
    )

    assert (  # nosec B101
        latest_component_changelog_version(
            changelog, upstream_version="2.15.3", suffix="aio"
        )
        == "v2.15.3-aio.2"
    )


def test_release_publish_target_allows_changelog_format_followup(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n\n- initial\n")
    _commit(tmp_path, "feat(test): initial")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v1.0.0-aio.1 - 2026-05-10\n\n- initial\n"
    )
    _commit(tmp_path, "chore(release): v1.0.0-aio.1")
    release_commit = _git_output(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v1.0.0-aio.1 - 2026-05-10\n\n- initial\n\n"
    )
    _commit(tmp_path, "chore(release): format test changelog")
    publish_commit = _git_output(tmp_path, "rev-parse", "HEAD")

    assert release_commit != publish_commit  # nosec B101
    assert (  # nosec B101
        find_release_publish_target_commit(tmp_path, "v1.0.0-aio.1") == publish_commit
    )


def test_release_publish_target_accepts_combined_component_release_commit(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n\n- initial\n")
    _commit(tmp_path, "feat(test): initial")
    (tmp_path / "CHANGELOG.md").write_text(
        "\n".join(
            [
                "## 0.153.0-agent.1 - 2026-05-28",
                "",
                "- agent",
                "",
                "## v0.126.0-aio.1 - 2026-05-28",
                "",
                "- aio",
                "",
            ]
        )
    )
    _commit(tmp_path, "chore(release): v0.126.0-aio.1 and 0.153.0-agent.1")
    release_commit = _git_output(tmp_path, "rev-parse", "HEAD")

    assert release_subject_matches(  # nosec B101
        "chore(release): v0.126.0-aio.1 and 0.153.0-agent.1 (#87)",
        "0.153.0-agent.1",
    )
    assert (  # nosec B101
        find_release_publish_target_commit(tmp_path, "v0.126.0-aio.1") == release_commit
    )
    assert (  # nosec B101
        find_release_publish_target_commit(tmp_path, "0.153.0-agent.1")
        == release_commit
    )


def test_release_publish_target_rejects_arbitrary_post_release_commit(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n\n- initial\n")
    _commit(tmp_path, "feat(test): initial")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v1.0.0-aio.1 - 2026-05-10\n\n- initial\n"
    )
    _commit(tmp_path, "chore(release): v1.0.0-aio.1")
    release_commit = _git_output(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("later\n")
    _commit(tmp_path, "fix(runtime): later change")

    assert (  # nosec B101
        find_release_publish_target_commit(tmp_path, "v1.0.0-aio.1") == release_commit
    )


def test_release_publish_target_rejects_changelog_subject_with_runtime_changes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## Unreleased\n\n- initial\n")
    _commit(tmp_path, "feat(test): initial")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v1.0.0-aio.1 - 2026-05-10\n\n- initial\n"
    )
    _commit(tmp_path, "chore(release): v1.0.0-aio.1")
    release_commit = _git_output(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "CHANGELOG.md").write_text(
        "## v1.0.0-aio.1 - 2026-05-10\n\n- initial\n\n"
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    _commit(tmp_path, "chore(release): format test changelog")

    assert (  # nosec B101
        find_release_publish_target_commit(tmp_path, "v1.0.0-aio.1") == release_commit
    )


def test_release_cli_supports_component_suffix(tmp_path: Path, capsys) -> None:
    _init_repo(tmp_path)
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "agent.Dockerfile").write_text(
        "ARG UPSTREAM_AGENT_VERSION=1.0.0\n"
    )
    (tmp_path / "components" / "agent.toml").write_text(
        '[upstream]\nversion_key = "UPSTREAM_AGENT_VERSION"\n'
    )
    (tmp_path / "components.toml").write_text("""
[components.agent]
dockerfile = "components/agent.Dockerfile"
upstream_config = "components/agent.toml"
release_suffix = "agent"
""")
    _commit(tmp_path, "feat(test): initial")

    assert (  # nosec B101
        main(["--repo-path", str(tmp_path), "--component", "agent", "next-version"])
        == 0
    )
    assert capsys.readouterr().out.strip() == "1.0.0-agent.1"  # nosec B101
