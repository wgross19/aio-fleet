from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from aio_fleet import github_writer
from aio_fleet.manifest import load_manifest


def test_contents_api_writer_requires_verified_commits(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and url.endswith("/git/ref/heads/codex/update"):
            return {"object": {"sha": "o" * 40}}
        if method == "PATCH":
            return {}
        if method == "GET" and "/contents/Dockerfile" in url:
            return {"sha": "blob"}
        if method == "PUT":
            return {"commit": {"sha": "n" * 40}}
        if method == "GET" and f"/commits/{'n' * 40}" in url:
            return {
                "commit": {"verification": {"verified": False, "reason": "unsigned"}}
            }
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(github_writer, "_github_request", fake_request)
    api_token = "dummy-token"  # nosec B105

    with pytest.raises(RuntimeError, match="not verified: unsigned"):
        github_writer.commit_paths_to_branch(
            repo,
            branch="codex/update",
            paths=["Dockerfile"],
            message="chore(sync): bump example",
            token=api_token,
        )

    restore = [
        payload
        for method, _url, payload in calls
        if method == "PATCH" and payload and payload.get("sha") == "o" * 40
    ]
    assert restore  # nosec B101


def test_contents_api_writer_returns_verified_head(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")

    def fake_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and url.endswith("/git/ref/heads/codex/update"):
            return {"object": {"sha": "h" * 40}}
        if method == "PATCH":
            return {}
        if method == "GET" and "/contents/Dockerfile" in url:
            return {"sha": "blob"}
        if method == "PUT":
            return {"commit": {"sha": "h" * 40}}
        if method == "GET" and f"/commits/{'h' * 40}" in url:
            return {"commit": {"verification": {"verified": True, "reason": "valid"}}}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(github_writer, "_github_request", fake_request)
    api_token = "dummy-token"  # nosec B105

    result = github_writer.commit_paths_to_branch(
        repo,
        branch="codex/update",
        paths=["Dockerfile"],
        message="chore(sync): bump example",
        token=api_token,
    )

    assert result.sha == "h" * 40  # nosec B101
    assert result.verified is True  # nosec B101
    assert result.method == "api"  # nosec B101


def test_api_writer_uses_atomic_git_data_commit_for_multiple_files(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
    (repo_path / ".aio-fleet.yml").write_text("repo: example-aio\n")
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and url.endswith("/git/ref/heads/codex/update"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if method == "GET" and url.endswith(f"/git/commits/{'m' * 40}"):
            return {"tree": {"sha": "t" * 40}}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "b" * 40}
        if method == "POST" and url.endswith("/git/trees"):
            assert payload is not None  # nosec B101
            assert len(payload["tree"]) == 2  # nosec B101
            return {"sha": "n" * 40}
        if method == "POST" and url.endswith("/git/commits"):
            assert payload is not None  # nosec B101
            assert "author" not in payload  # nosec B101
            assert "committer" not in payload  # nosec B101
            return {"sha": "c" * 40}
        if method == "POST" and url.endswith("/git/refs"):
            return {}
        if method == "GET" and f"/commits/{'c' * 40}" in url:
            return {"commit": {"verification": {"verified": True, "reason": "valid"}}}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(github_writer, "_github_request", fake_request)

    result = github_writer.commit_paths_to_branch(
        repo,
        branch="codex/update",
        paths=["Dockerfile", ".aio-fleet.yml"],
        message="chore(sync): update generated files",
        token="dummy-token",  # nosec B106
    )

    assert result.sha == "c" * 40  # nosec B101
    assert result.committed_paths == ["Dockerfile", ".aio-fleet.yml"]  # nosec B101
    assert not any(method == "PUT" for method, _url, _payload in calls)  # nosec B101


def test_api_writer_commits_submodule_gitlinks(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_writer._run_git(repo_path, ["init"])
    github_writer._run_git(
        repo_path,
        [
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{'b' * 40},openmemory",
        ],
    )
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=v2.0.1\n")
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and url.endswith("/git/ref/heads/codex/update"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if method == "GET" and url.endswith(f"/git/commits/{'m' * 40}"):
            return {"tree": {"sha": "t" * 40}}
        if method == "POST" and url.endswith("/git/blobs"):
            return {"sha": "d" * 40}
        if method == "POST" and url.endswith("/git/trees"):
            assert payload is not None  # nosec B101
            entries = payload["tree"]
            assert entries == [  # nosec B101
                {
                    "path": "Dockerfile",
                    "mode": "100644",
                    "type": "blob",
                    "sha": "d" * 40,
                },
                {
                    "path": "openmemory",
                    "mode": "160000",
                    "type": "commit",
                    "sha": "b" * 40,
                },
            ]
            return {"sha": "n" * 40}
        if method == "POST" and url.endswith("/git/commits"):
            assert payload is not None  # nosec B101
            assert "author" not in payload  # nosec B101
            assert "committer" not in payload  # nosec B101
            assert payload["parents"] == ["m" * 40]  # nosec B101
            return {"sha": "c" * 40}
        if method == "POST" and url.endswith("/git/refs"):
            return {}
        if method == "GET" and f"/commits/{'c' * 40}" in url:
            return {"commit": {"verification": {"verified": True, "reason": "valid"}}}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(github_writer, "_github_request", fake_request)
    api_token = "dummy-token"  # nosec B105

    result = github_writer.commit_paths_to_branch(
        repo,
        branch="codex/update",
        paths=["Dockerfile", "openmemory"],
        message="chore(sync): bump mem0",
        token=api_token,
    )

    assert result.sha == "c" * 40  # nosec B101
    assert result.verified is True  # nosec B101
    assert result.committed_paths == ["Dockerfile", "openmemory"]  # nosec B101
    assert any(
        url.endswith("/git/trees") for _method, url, _payload in calls
    )  # nosec B101


def test_run_git_uses_secretless_git_invocation(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setenv("AIO_FLEET_WORKFLOW_TOKEN", "workflow-token")
    monkeypatch.setenv("AIO_FLEET_CHECK_TOKEN", "check-token")
    monkeypatch.setattr(github_writer.subprocess, "run", fake_run)

    github_writer._run_git(repo_path, ["ls-files"], check=True)

    assert captured["args"] == [  # nosec B101
        shutil.which("git"),
        "-c",
        "core.fsmonitor=",
        "ls-files",
    ]
    env = captured["env"]
    assert isinstance(env, dict)  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in env  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in env  # nosec B101
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull  # nosec B101
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"  # nosec B101
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # nosec B101


def test_api_writer_commits_multiple_submodule_gitlinks(
    tmp_path: Path, monkeypatch
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_writer._run_git(repo_path, ["init"])
    github_writer._run_git(
        repo_path,
        [
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{'b' * 40},openmemory",
        ],
    )
    github_writer._run_git(
        repo_path,
        [
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{'c' * 40},providers",
        ],
    )
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")

    def fake_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "m" * 40}}
        if method == "GET" and url.endswith("/git/ref/heads/codex/update"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if method == "GET" and url.endswith(f"/git/commits/{'m' * 40}"):
            return {"tree": {"sha": "t" * 40}}
        if method == "POST" and url.endswith("/git/trees"):
            assert payload is not None  # nosec B101
            assert payload["tree"] == [  # nosec B101
                {
                    "path": "openmemory",
                    "mode": "160000",
                    "type": "commit",
                    "sha": "b" * 40,
                },
                {
                    "path": "providers",
                    "mode": "160000",
                    "type": "commit",
                    "sha": "c" * 40,
                },
            ]
            return {"sha": "n" * 40}
        if method == "POST" and url.endswith("/git/commits"):
            return {"sha": "d" * 40}
        if method == "POST" and url.endswith("/git/refs"):
            return {}
        if method == "GET" and f"/commits/{'d' * 40}" in url:
            return {"commit": {"verification": {"verified": True, "reason": "valid"}}}
        raise AssertionError((method, url, payload))

    monkeypatch.setattr(github_writer, "_github_request", fake_request)

    api_token = "dummy-token"  # nosec B105

    result = github_writer.commit_paths_to_branch(
        repo,
        branch="codex/update",
        paths=["openmemory", "providers"],
        message="chore(sync): bump submodules",
        token=api_token,
    )

    assert result.sha == "d" * 40  # nosec B101
    assert result.committed_paths == ["openmemory", "providers"]  # nosec B101


@pytest.mark.parametrize(
    ("path", "setup"),
    [
        ("/outside-secret", None),
        ("../secret", None),
        (
            "Dockerfile",
            lambda repo_path, tmp_path: (repo_path / "Dockerfile").unlink()
            or (repo_path / "Dockerfile").symlink_to(tmp_path / "outside-secret"),
        ),
    ],
)
def test_api_writer_rejects_unsafe_file_paths(
    tmp_path: Path, monkeypatch, path: str, setup
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "Dockerfile").write_text("ARG UPSTREAM_VERSION=1.1.0\n")
    (tmp_path / "outside-secret").write_text("secret\n")
    if setup:
        setup(repo_path, tmp_path)
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")

    def fake_request(*_args, **_kwargs) -> dict[str, object]:
        raise AssertionError("network should not be called for unsafe paths")

    monkeypatch.setattr(github_writer, "_github_request", fake_request)

    with pytest.raises(RuntimeError, match="invalid commit path"):
        github_writer.commit_paths_to_branch(
            repo,
            branch="codex/update",
            paths=[path],
            message="chore(sync): bump example",
            token="dummy-token",  # nosec B106
        )


def test_api_writer_rejects_unsafe_gitlink_paths(tmp_path: Path, monkeypatch) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    github_writer._run_git(repo_path, ["init"])
    github_writer._run_git(
        repo_path,
        [
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{'b' * 40},openmemory",
        ],
    )
    repo = load_manifest(_manifest(tmp_path, repo_path)).repo("example-aio")

    def fake_request(*_args, **_kwargs) -> dict[str, object]:
        raise AssertionError("network should not be called for unsafe paths")

    monkeypatch.setattr(github_writer, "_github_request", fake_request)

    with pytest.raises(RuntimeError, match="invalid commit path"):
        github_writer.commit_paths_to_branch(
            repo,
            branch="codex/update",
            paths=["../openmemory"],
            message="chore(sync): bump submodule",
            token="dummy-token",  # nosec B106
        )


def _manifest(tmp_path: Path, repo_path: Path) -> Path:
    manifest = tmp_path / "fleet.yml"
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
""")
    return manifest
