from __future__ import annotations

import json
from pathlib import Path

import yaml

from aio_fleet.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github" / "renovate" / "fleet.json"
WORKFLOW = ROOT / ".github" / "workflows" / "renovate.yml"


def test_renovate_config_targets_public_fleet_and_catalog_repos() -> None:
    manifest = load_manifest(ROOT / "fleet.yml")
    config = json.loads(CONFIG.read_text())

    expected = {
        repo.github_repo
        for repo in manifest.repos.values()
        if repo.raw.get("public") is True
    }
    expected.add(f"{manifest.owner}/aio-fleet")
    expected.add(str(manifest.raw["awesome_unraid_repository"]))

    assert set(config["repositories"]) == expected  # nosec B101
    assert config["repositories"] == sorted(config["repositories"])  # nosec B101
    assert all(
        repo.startswith("wgross19/") for repo in config["repositories"]
    )  # nosec B101


def test_renovate_config_is_central_low_noise_policy() -> None:
    config = json.loads(CONFIG.read_text())

    assert config["onboarding"] is False  # nosec B101
    assert config["requireConfig"] == "optional"  # nosec B101
    assert config["branchPrefix"] == "renovate/fleet/"  # nosec B101
    assert config["dependencyDashboard"] is True  # nosec B101
    assert config["dependencyDashboardAutoclose"] is True  # nosec B101
    assert config["timezone"] == "America/Phoenix"  # nosec B101
    assert config["schedule"] == ["* 2-6 * * 1"]  # nosec B101
    assert config["updateNotScheduled"] is False  # nosec B101
    assert config["prConcurrentLimit"] == 2  # nosec B101
    assert config["branchConcurrentLimit"] == 2  # nosec B101
    assert config["prHourlyLimit"] == 1  # nosec B101
    assert config["commitHourlyLimit"] == 2  # nosec B101
    assert "dockerfile" not in config["enabledManagers"]  # nosec B101
    assert "docker-compose" not in config["enabledManagers"]  # nosec B101


def test_renovate_workflow_runs_weekly_with_app_token() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    on_config = workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["renovate"]
    steps = job["steps"]
    step_names = [step["name"] for step in steps]
    resolve = _step(job, "Resolve GitHub App token")
    runtime = _step(job, "Prepare Renovate runtime")
    renovate = _step(job, "Run Renovate")

    assert on_config["schedule"] == [{"cron": "27 9 * * 1"}]  # nosec B101
    assert "pull_request" not in on_config  # nosec B101
    assert workflow["permissions"] == {"contents": "read"}  # nosec B101
    assert workflow["concurrency"]["cancel-in-progress"] is False  # nosec B101
    assert step_names.index("Enforce trusted ref for manual runs") < step_names.index(
        "Checkout aio-fleet"
    )  # nosec B101
    assert "AIO_FLEET_APP_PRIVATE_KEY" in resolve["env"]  # nosec B101
    assert "github_app --fallback-env GITHUB_TOKEN" in resolve["run"]  # nosec B101
    assert "RENOVATE_DRY_RUN=full" in runtime["run"]  # nosec B101
    assert renovate["uses"] == (  # nosec B101
        "renovatebot/github-action@693b9ef15eec82123529a37c782242f091365961"
    )
    assert renovate["with"]["configurationFile"] == (  # nosec B101
        ".github/renovate/fleet.json"
    )
    assert renovate["with"]["renovate-version"] == "43.209.1"  # nosec B101
    assert (
        renovate["with"]["token"] == "${{ steps.app-token.outputs.token }}"
    )  # nosec B101
    assert renovate["env"]["RENOVATE_GITHUB_COM_TOKEN"] == (  # nosec B101
        "${{ steps.app-token.outputs.token }}"
    )


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    for step in job["steps"]:  # type: ignore[index]
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")
