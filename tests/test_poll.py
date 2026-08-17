from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aio_fleet import poll
from aio_fleet.change_scope import CHECK_MODE_FAST_CLEANUP, CHECK_MODE_FULL
from aio_fleet.manifest import load_manifest


def test_poll_targets_skip_cross_repository_pull_requests(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 1,
                "headRefOid": "a" * 40,
                "isCrossRepository": True,
            },
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": ".trunk/trunk.yaml", "status": "modified"}],
    )

    targets = poll.poll_targets(manifest)

    assert [(target.sha, target.source) for target in targets] == [  # nosec B101
        ("b" * 40, "pr:2")
    ]


def test_poll_targets_require_same_repository_pr_identity(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 1,
                "headRefOid": "a" * 40,
                "headRepository": {"nameWithOwner": "someone-else/example-aio"},
            },
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "headRepository": {"nameWithOwner": "wgross19/example-aio"},
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": ".trunk/trunk.yaml", "status": "modified"}],
    )

    targets = poll.poll_targets(manifest)

    assert [(target.sha, target.source) for target in targets] == [  # nosec B101
        ("b" * 40, "pr:2")
    ]


def test_poll_targets_enable_configured_submodules_for_same_repo_prs(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path, checkout_submodules=True)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": ".trunk/trunk.yaml", "status": "modified"}],
    )
    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["Dockerfile"]
    )

    targets = poll.poll_targets(manifest)

    assert [target.event for target in targets] == [  # nosec B101
        "pull_request",
        "push",
    ]
    assert targets[0].checkout_submodules is True  # nosec B101
    assert targets[1].checkout_submodules is True  # nosec B101
    assert targets[1].publish is True  # nosec B101
    assert targets[1].publish_components == ("aio",)  # nosec B101


def test_poll_targets_mark_cleanup_prs_fast_path(tmp_path: Path, monkeypatch) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": ".trunk/trunk.yaml", "status": "modified"}],
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].check_mode == CHECK_MODE_FAST_CLEANUP  # nosec B101
    assert targets[0].changed_paths == (".trunk/trunk.yaml",)  # nosec B101
    assert targets[0].changed_files == (  # nosec B101
        {"path": ".trunk/trunk.yaml", "status": "modified"},
    )
    assert targets[0].fast_path_reason == (  # nosec B101
        "cleanup/local-hygiene-only paths"
    )


@pytest.mark.parametrize(
    ("changed_file", "expected_mode"),
    [
        ({"path": "scripts/release.py", "status": "removed"}, CHECK_MODE_FAST_CLEANUP),
        ({"path": "scripts/release.py", "status": "modified"}, CHECK_MODE_FULL),
    ],
)
def test_poll_targets_use_file_status_for_retired_cleanup_paths(
    tmp_path: Path,
    monkeypatch,
    changed_file: dict[str, str],
    expected_mode: str,
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll, "_pull_request_changed_files", lambda _repo, _number: [changed_file]
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].check_mode == expected_mode  # nosec B101
    assert targets[0].changed_paths == ("scripts/release.py",)  # nosec B101


@pytest.mark.parametrize(
    "changed_path",
    ["Dockerfile", "example-aio.xml", "tests/test_smoke.py", ".aio-fleet.yml"],
)
def test_poll_targets_keep_required_pr_paths_full(
    tmp_path: Path, monkeypatch, changed_path: str
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": changed_path, "status": "modified"}],
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].check_mode == CHECK_MODE_FULL  # nosec B101
    assert targets[0].changed_paths == (changed_path,)  # nosec B101


def test_poll_targets_fails_closed_when_pr_paths_cannot_be_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(
        poll,
        "_open_pull_requests",
        lambda _repo: [
            {
                "number": 2,
                "headRefOid": "b" * 40,
                "isCrossRepository": False,
            },
        ],
    )
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "")
    monkeypatch.setattr(
        poll, "_pull_request_changed_files", lambda _repo, _number: None
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].check_mode == CHECK_MODE_FULL  # nosec B101
    assert targets[0].changed_paths == ()  # nosec B101
    assert targets[0].fast_path_reason == "changed paths unresolved"  # nosec B101


def test_publish_required_ignores_docs_only_main_commits(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["docs/a.md"]
    )

    assert (
        poll.publish_required(repo, sha="a" * 40, event="push") is False
    )  # nosec B101


def test_publish_required_ignores_resolved_empty_commits(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll,
        "_gh",
        lambda _command: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert poll._commit_changed_files(repo, "a" * 40) == []  # nosec B101
    assert poll._commit_changed_paths(repo, "a" * 40) == []  # nosec B101
    assert (
        poll.publish_components_required(repo, sha="a" * 40, event="push")  # nosec B101
        == []
    )


def test_publish_required_accepts_runtime_and_release_commits(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    for path in ("Dockerfile", "rootfs/etc/services.d/web/run", "CHANGELOG.md"):
        monkeypatch.setattr(
            poll,
            "_commit_changed_paths",
            lambda _repo, _sha, changed_path=path: [changed_path],
        )

        assert (
            poll.publish_required(repo, sha="a" * 40, event="push") is True
        )  # nosec B101


def test_poll_targets_skip_publish_for_docs_only_main_commits(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(poll, "_open_pull_requests", lambda _repo: [])
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["README.md"]
    )
    monkeypatch.setattr(poll, "_recent_main_publish_components", lambda _repo: [])

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].publish is False  # nosec B101
    assert targets[0].publish_components == ()  # nosec B101


def test_poll_targets_mark_cleanup_only_main_commits_fast_path(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(poll, "_open_pull_requests", lambda _repo: [])
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: [".trunk/trunk.yaml"]
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].publish is False  # nosec B101
    assert targets[0].check_mode == CHECK_MODE_FAST_CLEANUP  # nosec B101


def test_poll_targets_publish_main_commits_for_publish_related_paths(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(poll, "_open_pull_requests", lambda _repo: [])
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["Dockerfile"]
    )

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].publish is True  # nosec B101
    assert targets[0].publish_components == ("aio",)  # nosec B101


def test_poll_targets_skip_publish_when_main_paths_cannot_be_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(poll, "_open_pull_requests", lambda _repo: [])
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(poll, "_commit_changed_paths", lambda _repo, _sha: None)

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].publish is False  # nosec B101
    assert targets[0].publish_components == ()  # nosec B101


def test_poll_targets_publish_when_recent_main_history_has_runtime_changes(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    manifest = load_manifest(manifest_path)

    monkeypatch.setattr(poll, "_open_pull_requests", lambda _repo: [])
    monkeypatch.setattr(poll, "_main_sha", lambda _repo: "c" * 40)
    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["README.md"]
    )
    monkeypatch.setattr(poll, "_recent_main_publish_components", lambda _repo: ["aio"])

    targets = poll.poll_targets(manifest)

    assert len(targets) == 1  # nosec B101
    assert targets[0].publish is True  # nosec B101
    assert targets[0].publish_components == ("aio",)  # nosec B101


def test_recent_main_publish_components_ignores_already_checked_history(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")
    inspected: list[str] = []

    monkeypatch.setattr(
        poll,
        "_main_commit_shas",
        lambda _repo, *, limit=20: ["head", "checked", "runtime"],
    )
    monkeypatch.setattr(
        poll,
        "check_run_satisfied",
        lambda _repo, *, sha, event: sha == "checked" and event == "push",
    )

    def fake_commit_changed_paths(_repo, sha: str):
        inspected.append(sha)
        return ["Dockerfile"]

    monkeypatch.setattr(poll, "_commit_changed_paths", fake_commit_changed_paths)

    assert poll._recent_main_publish_components(repo) == []  # nosec B101
    assert inspected == []  # nosec B101


def test_recent_main_publish_components_scans_until_checked_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll,
        "_main_commit_shas",
        lambda _repo, *, limit=20: ["head", "runtime", "docs", "checked"],
    )
    monkeypatch.setattr(
        poll,
        "check_run_satisfied",
        lambda _repo, *, sha, event: sha == "checked" and event == "push",
    )
    monkeypatch.setattr(
        poll,
        "_commit_changed_paths",
        lambda _repo, sha: {
            "runtime": ["Dockerfile"],
            "docs": ["README.md"],
        }[sha],
    )

    assert poll._recent_main_publish_components(repo) == ["aio"]  # nosec B101


def test_recent_main_publish_components_fails_safe_on_unresolved_history(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path, include_agent_component=True)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll,
        "_main_commit_shas",
        lambda _repo, *, limit=20: ["head", "unresolved"],
    )
    monkeypatch.setattr(
        poll,
        "check_run_satisfied",
        lambda _repo, *, sha, event: False,
    )
    monkeypatch.setattr(poll, "_commit_changed_paths", lambda _repo, _sha: None)

    assert poll._recent_main_publish_components(repo) == [  # nosec B101
        "aio",
        "agent",
    ]


def test_publish_components_required_can_target_alpha_component(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path, include_alpha_component=True)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["Dockerfile.alpha"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["sure-alpha"]

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["sure-aio-alpha.xml"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["sure-alpha"]

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["CHANGELOG.alpha.md"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["sure-alpha"]

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["Dockerfile"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["aio"]

    monkeypatch.setattr(poll, "_commit_changed_paths", lambda _repo, _sha: None)

    with pytest.raises(poll.PublishPathResolutionError):
        poll.publish_components_required(repo, sha="a" * 40, event="push")

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["example-aio.xml"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["aio"]


def test_publish_components_required_targets_generic_multi_component_paths(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path, include_agent_component=True)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll, "_commit_changed_paths", lambda _repo, _sha: ["Dockerfile"]
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["aio"]

    monkeypatch.setattr(
        poll,
        "_commit_changed_paths",
        lambda _repo, _sha: ["components/example-agent/Dockerfile"],
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["agent"]

    monkeypatch.setattr(
        poll,
        "_commit_changed_paths",
        lambda _repo, _sha: ["Dockerfile", "CHANGELOG.md"],
    )

    assert poll.publish_components_required(  # nosec B101
        repo, sha="a" * 40, event="push"
    ) == ["aio", "agent"]


def test_poll_gh_maps_app_token_to_gh_token(monkeypatch) -> None:
    captured_env: dict[str, str] = {}

    def fake_run(_command: list[str], **kwargs):
        nonlocal captured_env
        captured_env = dict(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setenv("APP_TOKEN", "app-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(poll.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(poll.subprocess, "run", fake_run)

    result = poll._gh(["pr", "list"])

    assert result.returncode == 0  # nosec B101
    assert captured_env["GH_TOKEN"] == "app-token"  # nosec B101
    assert "GITHUB_TOKEN" not in captured_env  # nosec B101


def test_pull_request_changed_files_preserves_previous_filename(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")
    captured_command: list[str] = []

    def fake_gh(command: list[str]):
        nonlocal captured_command
        captured_command = list(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"path":"docs/Dockerfile","status":"renamed",'
                '"previous_path":"Dockerfile"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(poll, "_gh", fake_gh)

    changed_files = poll._pull_request_changed_files(repo, "123")

    assert changed_files == [  # nosec B101
        {"path": "docs/Dockerfile", "status": "renamed"},
        {"path": "Dockerfile", "status": "renamed-from"},
    ]
    assert any(  # nosec B101
        "previous_path: .previous_filename" in arg for arg in captured_command
    )


def test_commit_changed_files_preserves_previous_filename(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")
    captured_command: list[str] = []

    def fake_gh(command: list[str]):
        nonlocal captured_command
        captured_command = list(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"path":"docs/Dockerfile","status":"renamed",'
                '"previous_path":"Dockerfile"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(poll, "_gh", fake_gh)

    changed_files = poll._commit_changed_files(repo, "a" * 40)

    assert changed_files == [  # nosec B101
        {"path": "docs/Dockerfile", "status": "renamed"},
        {"path": "Dockerfile", "status": "renamed-from"},
    ]
    assert any(  # nosec B101
        "previous_path: .previous_filename" in arg for arg in captured_command
    )


def test_pull_request_changed_files_ignores_null_previous_filename(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(
        poll,
        "_gh",
        lambda _command: SimpleNamespace(
            returncode=0,
            stdout='{"path":"docs/support.md","status":"modified","previous_path":null}\n',
            stderr="",
        ),
    )

    assert poll._pull_request_changed_files(repo, "123") == [  # nosec B101
        {"path": "docs/support.md", "status": "modified"}
    ]


def _write_manifest(
    tmp_path: Path,
    *,
    checkout_submodules: bool = False,
    include_alpha_component: bool = False,
    include_agent_component: bool = False,
) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    checkout_line = "    checkout_submodules: true\n" if checkout_submodules else ""
    manifest = tmp_path / "fleet.yml"
    alpha_components = (
        """
    components:
      aio:
        image_name: wgross19/example-aio
        dockerfile: Dockerfile
        xml_paths:
          - example-aio.xml
      sure-alpha:
        image_name: wgross19/example-aio-alpha
        dockerfile: Dockerfile.alpha
        release_changelog: CHANGELOG.alpha.md
        xml_paths:
          - sure-aio-alpha.xml
        publish_paths:
          - rootfs-alpha/**
"""
        if include_alpha_component
        else ""
    )
    agent_components = (
        """
    publish_profile: multi-component
    components:
      aio:
        image_name: wgross19/example-aio
        dockerfile: Dockerfile
      agent:
        image_name: wgross19/example-agent
        dockerfile: components/example-agent/Dockerfile
        context: components/example-agent
        release_changelog: CHANGELOG.md
"""
        if include_agent_component
        else ""
    )
    manifest.write_text(f"""
owner: wgross19
repos:
  example-aio:
    path: {repo_path}
    public: true
    app_slug: example-aio
    image_name: wgross19/example-aio
    docker_cache_scope: example-aio-image
    pytest_image_tag: example-aio:pytest
{checkout_line.rstrip()}
{alpha_components.rstrip()}
{agent_components.rstrip()}
""")
    return manifest


def test_resolve_changed_files_uses_head_verified_pull_request_files(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")
    seen_numbers: list[str] = []

    monkeypatch.setattr(
        poll,
        "_commit_changed_files",
        lambda *_args, **_kwargs: pytest.fail("commit resolver should not be used"),
    )

    def fake_pull_request_head_sha(_repo, number: str) -> str:
        seen_numbers.append(number)
        return "a" * 40

    def fake_pull_request_changed_files(_repo, number: str):
        seen_numbers.append(number)
        return [{"path": "Dockerfile", "status": "modified"}]

    monkeypatch.setattr(poll, "_pull_request_head_sha", fake_pull_request_head_sha)
    monkeypatch.setattr(
        poll, "_pull_request_changed_files", fake_pull_request_changed_files
    )

    changed = poll.resolve_changed_files(
        repo,
        sha="a" * 40,
        event="pull_request",
        source="pr:42",
    )

    assert changed == [{"path": "Dockerfile", "status": "modified"}]  # nosec B101
    assert seen_numbers == ["42", "42", "42"]  # nosec B101


def test_resolve_changed_files_fails_closed_when_pr_head_mismatches_check_sha(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")

    monkeypatch.setattr(poll, "_pull_request_head_sha", lambda _repo, _number: "b" * 40)
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda *_args, **_kwargs: pytest.fail("PR files should not be trusted"),
    )

    assert (  # nosec B101
        poll.resolve_changed_files(
            repo,
            sha="a" * 40,
            event="pull_request",
            source="pr:42",
        )
        is None
    )


def test_resolve_changed_files_fails_closed_when_pr_head_moves_during_lookup(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = _write_manifest(tmp_path)
    repo = load_manifest(manifest_path).repo("example-aio")
    head_shas = iter(["a" * 40, "b" * 40])

    monkeypatch.setattr(
        poll,
        "_pull_request_head_sha",
        lambda _repo, _number: next(head_shas),
    )
    monkeypatch.setattr(
        poll,
        "_pull_request_changed_files",
        lambda _repo, _number: [{"path": "Dockerfile", "status": "modified"}],
    )

    assert (  # nosec B101
        poll.resolve_changed_files(
            repo,
            sha="a" * 40,
            event="pull_request",
            source="pr:42",
        )
        is None
    )
