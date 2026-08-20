from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from aio_fleet import checks
from aio_fleet.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "fleet.yml"


def test_check_external_id_uses_repo_sha_and_policy_hash() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    external_id = checks.check_external_id(repo, sha="a" * 40, event="push")

    assert external_id.startswith("sure-aio:" + "a" * 40 + ":")  # nosec B101
    assert len(external_id.rsplit(":", 1)[1]) == 16  # nosec B101


def test_check_run_payload_builds_required_check() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    payload = checks.check_run_payload(
        repo,
        sha="b" * 40,
        event="pull_request",
        status="completed",
        conclusion="success",
        summary="All central checks passed.",
        details_url="https://github.com/wgross19/aio-fleet/actions/runs/1",
    )

    assert payload["name"] == "aio-fleet / required"  # nosec B101
    assert payload["head_sha"] == "b" * 40  # nosec B101
    assert payload["conclusion"] == "success"  # nosec B101
    assert payload["output"]["summary"] == "All central checks passed."  # nosec B101
    assert payload["details_url"].endswith("/1")  # nosec B101


def test_check_run_payload_rejects_in_progress_conclusion() -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    with pytest.raises(ValueError, match="conclusion is only valid"):
        checks.check_run_payload(
            repo,
            sha="c" * 40,
            event="push",
            status="in_progress",
            conclusion="success",
        )


def test_upsert_check_run_updates_matching_external_id(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")
    payload = checks.check_run_payload(
        repo,
        sha="d" * 40,
        event="push",
        status="completed",
        conclusion="success",
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_github_request(
        url: str,
        *,
        token: str,
        method: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        calls.append((url, method, payload))
        if method == "GET":
            return {
                "check_runs": [
                    {
                        "id": 123,
                        "external_id": payload_external_id,
                        "html_url": "https://example.invalid/old",
                    }
                ]
            }
        assert method == "PATCH"  # nosec B101
        assert payload and "head_sha" not in payload  # nosec B101
        return {"id": 123, "html_url": "https://example.invalid/new"}

    payload_external_id = str(payload["external_id"])
    monkeypatch.setattr(checks, "_github_request", fake_github_request)

    result = checks.upsert_check_run(
        repo,
        sha="d" * 40,
        event="push",
        status="completed",
        conclusion="success",
        token="token",  # nosec B106
    )

    assert result.action == "updated"  # nosec B101
    assert result.check_run_id == 123  # nosec B101
    assert [call[1] for call in calls] == ["GET", "PATCH"]  # nosec B101


def test_check_run_satisfied_requires_completed_success(monkeypatch) -> None:
    repo = load_manifest(FIXTURE).repo("sure-aio")

    monkeypatch.setattr(
        checks,
        "_github_request",
        lambda *args, **kwargs: {
            "check_runs": [
                {
                    "id": 123,
                    "external_id": checks.check_external_id(
                        repo, sha="e" * 40, event="pull_request"
                    ),
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
    )

    assert checks.check_run_satisfied(  # nosec B101
        repo, sha="e" * 40, event="pull_request", token="token"  # nosec B106
    )


def test_check_run_forbidden_error_is_concise(monkeypatch) -> None:
    def fake_read(*_args: object, **_kwargs: object):
        raise urllib.error.HTTPError(
            "https://api.github.com/repos/wgross19/nanoclaw-aio/check-runs",
            403,
            "Forbidden",
            {},
            None,
        )

    monkeypatch.setattr(checks, "read_urlopen_with_retry", fake_read)

    with pytest.raises(RuntimeError, match="Checks: write access"):
        checks._github_request(  # noqa: SLF001
            "https://api.github.com/repos/wgross19/nanoclaw-aio/check-runs",
            token="token",  # nosec B106
            method="POST",
            payload={},
        )
