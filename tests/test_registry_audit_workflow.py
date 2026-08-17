from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "registry-audit.yml"
SECRET_ENV_KEYS = {
    "APP_TOKEN",
    "AIO_FLEET_WORKFLOW_TOKEN",
    "AIO_FLEET_APP_ID",
    "AIO_FLEET_APP_INSTALLATION_ID",
    "AIO_FLEET_APP_PRIVATE_KEY",
    "AIO_FLEET_KUMA_PUSH_URL",
    "AIO_FLEET_ALERT_WEBHOOK_URL",
}


def test_registry_audit_scopes_secrets_to_required_steps() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["registry-audit"]

    assert not SECRET_ENV_KEYS.intersection(job.get("env", {}))  # nosec B101

    token_step = _step(job, "Resolve GitHub App token")
    assert {  # nosec B101
        "AIO_FLEET_APP_CLIENT_ID",
        "AIO_FLEET_APP_ID",
        "AIO_FLEET_APP_INSTALLATION_ID",
        "AIO_FLEET_APP_PRIVATE_KEY",
    }.issubset(token_step["env"])
    assert token_step["env"]["AIO_FLEET_APP_CLIENT_ID"] == (  # nosec B101
        "${{ vars.AIO_FLEET_APP_CLIENT_ID }}"
    )

    alert_step = _step(job, "Alert registry audit")
    assert {  # nosec B101
        "AIO_FLEET_KUMA_PUSH_URL",
        "AIO_FLEET_ALERT_WEBHOOK_URL",
    }.issubset(alert_step["env"])


def test_registry_audit_manual_runs_require_default_branch_before_checkout() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["registry-audit"]
    names = [step["name"] for step in job["steps"]]
    guard = _step(job, "Enforce trusted ref for manual runs")

    assert names.index(
        "Enforce trusted ref for manual runs"
    ) < names.index(  # nosec B101
        "Checkout"
    )
    assert names.index(
        "Enforce trusted ref for manual runs"
    ) < names.index(  # nosec B101
        "Resolve GitHub App token"
    )
    assert (
        guard["if"] == "${{ github.event_name == 'workflow_dispatch' }}"
    )  # nosec B101
    assert (
        "github.event.repository.default_branch" in guard["env"]["EXPECTED_REF"]
    )  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" not in guard.get("env", {})  # nosec B101
    assert "GITHUB_REF" in guard["run"]  # nosec B101


def test_registry_audit_sanitizes_verify_subprocess_environment() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    verify = _step(workflow["jobs"]["registry-audit"], "Verify registry tags")
    summary = _step(workflow["jobs"]["registry-audit"], "Publish registry summary")
    alert = _step(workflow["jobs"]["registry-audit"], "Alert registry audit")

    assert "APP_TOKEN" not in verify.get("env", {})  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" in verify.get("env", {})  # nosec B101
    assert 'os.environ["APP_TOKEN"]' not in verify["run"]  # nosec B101
    assert "workflow registry-audit" in verify["run"]  # nosec B101
    assert "registry-audit.err" in verify["run"]  # nosec B101
    assert "GIT_CONFIG_KEY_0" not in verify["run"]  # nosec B101
    assert "GIT_CONFIG_VALUE_0" not in verify["run"]  # nosec B101
    assert "extraheader=AUTHORIZATION" not in verify["run"]  # nosec B101
    assert "${{ steps.app-token.outputs.token }}" not in verify["run"]  # nosec B101
    assert summary["if"] == "${{ always() }}"  # nosec B101
    assert "did not produce a report" in summary["run"]  # nosec B101
    assert "--failure-file registry-audit.err" in alert["run"]  # nosec B101


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")
