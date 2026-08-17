from __future__ import annotations

import pytest

from aio_fleet.failure_classifier import classify_failure_text, classify_workflow_state


@pytest.mark.parametrize(
    ("snippet", "root_cause"),
    [
        (
            "permission_denied: write_package for ghcr.io/wgross19/sure-aio",
            "ghcr-access",
        ),
        ("Docker Hub publish credentials are missing", "dockerhub-auth"),
        (
            "missing or unreachable registry tags: wgross19/sure-aio:latest",
            "registry-tags-missing",
        ),
        (
            "fetch tags before trusting release due from a shallow checkout",
            "release-history-incomplete",
        ),
        (
            "required-check-missing: aio-fleet / required did not pass",
            "required-check-missing",
        ),
        (
            "fmt Incorrect formatting, autoformat by running 'trunk fmt' prettier",
            "formatting",
        ),
        (
            "Command '['docker', 'build', '-f', 'Dockerfile', '.']' "
            "returned non-zero exit status 1.",
            "image-build",
        ),
        ("error: patch failed: src/container-runner.ts:22", "image-build"),
        ("unexpected upstream gem versions: {'addressable': '2.8.7'}", "upstream-pin"),
        (
            "gh: To use GitHub CLI in a GitHub Actions workflow, set the "
            "GH_TOKEN environment variable",
            "release-publish-token",
        ),
        ("base branch policy prohibits the merge", "signed-commit"),
        ("pytest failed in tests/integration/test_runtime.py", "integration-test"),
        ("catalog-sync-needed for awesome-unraid XML", "catalog-drift"),
        ("Resource not accessible by integration", "github-app-permission"),
        (
            "upstream monitor blocked: missing configured submodule ref",
            "upstream-blocked",
        ),
        ("workflow timed out after 60 minutes", "workflow-timeout"),
    ],
)
def test_classify_known_failure_modes(snippet: str, root_cause: str) -> None:
    result = classify_failure_text(snippet, metadata={"run_id": "12345"})

    assert result["root_cause"] == root_cause  # nosec B101
    assert result["run_id"] == "12345"  # nosec B101
    assert result["next_action"]  # nosec B101


def test_classification_redacts_public_text() -> None:
    result = classify_failure_text(
        "pytest failed from /Users/shadowbook/Documents/aio-fleet/.venv/bin/python",
        metadata={"run_id": "12345"},
    )

    assert "/Users/shadowbook" not in str(result)  # nosec B101
    assert result["summary"] == "integration-test failure detected"  # nosec B101


def test_workflow_classification_ignores_recovered_failure() -> None:
    failures = classify_workflow_state(
        {
            "repo": "wgross19/aio-fleet",
            "state": "success",
            "latest": {
                "id": 200,
                "conclusion": "success",
                "updated_at": "2026-05-22T15:21:44Z",
            },
            "last_failure": {
                "id": 100,
                "conclusion": "failure",
                "updated_at": "2026-05-22T13:15:04Z",
                "title": "AIO Fleet Control Plane",
            },
        }
    )

    assert failures == []  # nosec B101


def test_workflow_classification_keeps_current_failure() -> None:
    failures = classify_workflow_state(
        {
            "repo": "wgross19/aio-fleet",
            "state": "failure",
            "latest": {
                "id": 200,
                "conclusion": "failure",
                "updated_at": "2026-05-22T15:21:44Z",
            },
            "last_failure": {
                "id": 200,
                "conclusion": "failure",
                "updated_at": "2026-05-22T15:21:44Z",
                "title": "AIO Fleet Control Plane",
            },
        }
    )

    assert len(failures) == 1  # nosec B101
    assert failures[0]["run_id"] == "200"  # nosec B101


def test_workflow_classification_does_not_hide_unrelated_success() -> None:
    failures = classify_workflow_state(
        {
            "repo": "wgross19/aio-fleet",
            "state": "success",
            "latest": {
                "id": 200,
                "conclusion": "success",
                "event": "schedule",
                "title": "fleet-dashboard",
                "updated_at": "2026-05-22T15:21:44Z",
            },
            "last_failure": {
                "id": 100,
                "conclusion": "failure",
                "event": "workflow_dispatch",
                "title": "control-check",
                "updated_at": "2026-05-22T13:15:04Z",
            },
        }
    )

    assert len(failures) == 1  # nosec B101
    assert failures[0]["run_id"] == "100"  # nosec B101


def test_workflow_classification_ignores_recovered_scheduled_failure() -> None:
    failures = classify_workflow_state(
        {
            "repo": "wgross19/aio-fleet",
            "state": "success",
            "latest": {
                "id": 200,
                "conclusion": "success",
                "event": "workflow_dispatch",
                "branch": "main",
                "title": "AIO Fleet Control Plane",
                "updated_at": "2026-06-02T20:21:44Z",
            },
            "last_failure": {
                "id": 100,
                "conclusion": "failure",
                "event": "schedule",
                "branch": "main",
                "title": "AIO Fleet Control Plane",
                "updated_at": "2026-06-02T13:15:04Z",
            },
        }
    )

    assert failures == []  # nosec B101


def test_workflow_classification_uses_last_success_during_dashboard_refresh() -> None:
    failures = classify_workflow_state(
        {
            "repo": "wgross19/aio-fleet",
            "state": "in_progress",
            "latest": {
                "id": 300,
                "status": "in_progress",
                "conclusion": "",
                "event": "workflow_dispatch",
                "branch": "main",
                "title": "AIO Fleet Control Plane",
                "updated_at": "2026-06-18T22:04:46Z",
            },
            "last_success": {
                "id": 200,
                "conclusion": "success",
                "event": "workflow_dispatch",
                "branch": "main",
                "title": "AIO Fleet Control Plane",
                "updated_at": "2026-06-18T22:00:30Z",
            },
            "last_failure": {
                "id": 100,
                "conclusion": "failure",
                "event": "workflow_dispatch",
                "branch": "main",
                "title": "AIO Fleet Control Plane",
                "updated_at": "2026-06-18T21:48:43Z",
            },
        }
    )

    assert failures == []  # nosec B101
