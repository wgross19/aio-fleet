from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "control-plane.yml"
PUBLISH_ACTION = ROOT / ".github" / "actions" / "publish-registry-images" / "action.yml"
SECRET_ENV_KEYS = {
    "AIO_FLEET_APP_ID",
    "AIO_FLEET_APP_INSTALLATION_ID",
    "AIO_FLEET_APP_PRIVATE_KEY",
    "AIO_FLEET_CHECK_TOKEN",
    "AIO_FLEET_DASHBOARD_TOKEN",
    "AIO_FLEET_GHCR_TOKEN",
    "AIO_FLEET_ISSUE_TOKEN",
    "AIO_FLEET_KUMA_PUSH_URL",
    "AIO_FLEET_RELEASE_TOKEN",
    "AIO_FLEET_ALERT_WEBHOOK_URL",
    "AIO_FLEET_UPSTREAM_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}
APP_CODE_SECRET_ENV_KEYS = SECRET_ENV_KEYS - {
    "AIO_FLEET_GHCR_TOKEN",
}
REGISTRY_PUBLISH_ENV_KEYS = {
    "AIO_FLEET_REGISTRY_AUTH_MODE",
}
REGISTRY_PUBLISH_SECRET_ENV_KEYS = {
    "DOCKERHUB_USERNAME",
    "DOCKERHUB_TOKEN",
    "AIO_FLEET_GHCR_TOKEN",
    "AIO_FLEET_GHCR_USERNAME",
}


def test_poll_checks_job_does_not_export_secrets_to_app_code_step() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    poll_checks = workflow["jobs"]["poll-checks"]
    publish_job = workflow["jobs"]["poll-registry-publish"]

    assert not SECRET_ENV_KEYS.intersection(poll_checks.get("env", {}))  # nosec B101

    run_step = _step(poll_checks, "Run central control check")
    publish_step = _step(publish_job, "Publish registry images")
    assert run_step.get("continue-on-error") is True  # nosec B101
    assert not APP_CODE_SECRET_ENV_KEYS.intersection(
        run_step.get("env", {})
    )  # nosec B101
    assert not REGISTRY_PUBLISH_ENV_KEYS.intersection(
        run_step.get("env", {})
    )  # nosec B101
    assert REGISTRY_PUBLISH_ENV_KEYS.issubset(publish_step.get("env", {}))  # nosec B101
    assert not REGISTRY_PUBLISH_SECRET_ENV_KEYS.intersection(
        publish_step.get("env", {})
    )  # nosec B101
    assert "--check-run" not in run_step["run"]  # nosec B101


def test_control_plane_manual_runs_require_default_branch_before_checkout() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["control-plane"]
    names = [step["name"] for step in job["steps"]]
    guard = _step(job, "Enforce trusted ref for manual runs")

    assert names.index(
        "Enforce trusted ref for manual runs"
    ) < names.index(  # nosec B101
        "Checkout aio-fleet"
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


def test_upstream_monitor_scopes_git_auth_without_standard_tokens() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    checkout = _step(
        workflow["jobs"]["control-plane"], "Checkout upstream monitor repos"
    )
    monitor = _step(workflow["jobs"]["control-plane"], "Monitor upstream releases")
    restore = _step(
        workflow["jobs"]["control-plane"],
        "Restore trusted aio-fleet after upstream monitor",
    )
    validate = _step(
        workflow["jobs"]["control-plane"], "Validate upstream monitor handoff"
    )
    apply = _step(workflow["jobs"]["control-plane"], "Apply upstream monitor actions")

    assert "AIO_FLEET_WORKFLOW_TOKEN" in checkout["env"]  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in checkout["env"]  # nosec B101
    assert "workflow checkout-upstream" in checkout["run"]  # nosec B101
    assert "${RUNNER_TEMP}/upstream-monitor" in checkout["run"]  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in monitor.get("env", {})  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in monitor.get("env", {})  # nosec B101
    assert "GH_TOKEN" not in monitor["env"]  # nosec B101
    assert "GITHUB_TOKEN" not in monitor["env"]  # nosec B101
    assert "workflow upstream-monitor" in monitor["run"]  # nosec B101
    assert "${RUNNER_TEMP}/upstream-monitor" in monitor["run"]  # nosec B101
    assert "env -i" in monitor["run"]  # nosec B101
    assert 'HOME="${monitor_home}"' in monitor["run"]  # nosec B101
    assert "mktemp -d" in monitor["run"]  # nosec B101
    assert "git reset --hard HEAD" in restore["run"]  # nosec B101
    assert "git clean -ffdx" in restore["run"]  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in restore.get("env", {})  # nosec B101
    assert restore["env"]["PYTHONNOUSERSITE"] == "1"  # nosec B101
    assert "workflow upstream-validate" in validate["run"]  # nosec B101
    assert "validated-report.json" in validate["run"]  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" not in validate.get("env", {})  # nosec B101
    assert validate["env"]["PYTHONNOUSERSITE"] == "1"  # nosec B101
    assert "AIO_FLEET_WORKFLOW_TOKEN" in apply["env"]  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" in apply["env"]  # nosec B101
    assert apply["env"]["PYTHONNOUSERSITE"] == "1"  # nosec B101
    assert "workflow upstream-actions" in apply["run"]  # nosec B101
    assert "--manifest fleet.yml" in apply["run"]  # nosec B101
    assert "--checkout-root" in apply["run"]  # nosec B101
    assert "validated-report.json" in apply["run"]  # nosec B101
    assert "extraheader=AUTHORIZATION" not in monitor["run"]  # nosec B101
    assert '"config",' not in monitor["run"]  # nosec B101


def test_dashboard_checkout_does_not_put_auth_header_in_git_argv() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    dashboard = _step(workflow["jobs"]["control-plane"], "Checkout dashboard repos")

    assert "workflow checkout-dashboard" in dashboard["run"]  # nosec B101
    assert "standards-reconcile" in dashboard["if"]  # nosec B101
    assert "extraheader=AUTHORIZATION" not in dashboard["run"]  # nosec B101
    assert '"config",' not in dashboard["run"]  # nosec B101


def test_dashboard_update_scopes_dashboard_tokens() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    dashboard = _step(workflow["jobs"]["control-plane"], "Update fleet dashboard issue")

    assert "AIO_FLEET_DASHBOARD_TOKEN" in dashboard["env"]  # nosec B101
    assert "AIO_FLEET_UPSTREAM_TOKEN" in dashboard["env"]  # nosec B101
    assert "AIO_FLEET_ISSUE_TOKEN" in dashboard["env"]  # nosec B101
    assert "APP_TOKEN" not in dashboard["env"]  # nosec B101
    assert "GH_TOKEN" not in dashboard["env"]  # nosec B101
    assert "GITHUB_TOKEN" not in dashboard["env"]  # nosec B101


def test_alert_test_mode_uses_alert_webhook_secret_only() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    on_config = workflow.get("on", workflow.get(True))
    mode = on_config["workflow_dispatch"]["inputs"]["mode"]
    app_token = _step(workflow["jobs"]["control-plane"], "Resolve GitHub App token")
    alert_test = _step(workflow["jobs"]["control-plane"], "Test alert webhook")

    assert app_token["env"]["AIO_FLEET_APP_CLIENT_ID"] == (  # nosec B101
        "${{ vars.AIO_FLEET_APP_CLIENT_ID }}"
    )
    assert "alert-test" in mode["options"]  # nosec B101
    assert "inputs.mode != 'alert-test'" in app_token["if"]  # nosec B101
    assert (  # nosec B101
        alert_test["if"]
        == "${{ github.event_name == 'workflow_dispatch' && inputs.mode == 'alert-test' }}"
    )
    assert alert_test["env"] == {  # nosec B101
        "AIO_FLEET_ALERT_WEBHOOK_URL": "${{ secrets.AIO_FLEET_ALERT_WEBHOOK_URL }}",
        "DETAILS_URL": "https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}",
    }
    assert (
        "alert doctor --require-alerts --format json" in alert_test["run"]
    )  # nosec B101
    assert "alert test" in alert_test["run"]  # nosec B101
    assert "aio-fleet Discord alert test" in alert_test["run"]  # nosec B101
    assert "--dry-run" not in alert_test["run"]  # nosec B101


def test_dockerhub_tag_cleanup_mode_is_guarded() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    on_config = workflow.get("on", workflow.get(True))
    inputs = on_config["workflow_dispatch"]["inputs"]
    mode = inputs["mode"]
    cleanup = _step(workflow["jobs"]["control-plane"], "Delete Docker Hub tags")

    assert len(inputs) <= 10  # nosec B101
    assert "dockerhub-tag-cleanup" in mode["options"]  # nosec B101
    assert "dockerhub_image" in inputs  # nosec B101
    assert "dockerhub_tags" in inputs  # nosec B101
    assert cleanup["if"] == (  # nosec B101
        "${{ github.event_name == 'workflow_dispatch' && "
        "inputs.mode == 'dockerhub-tag-cleanup' }}"
    )
    assert cleanup["env"]["DOCKERHUB_USERNAME"] == (  # nosec B101
        "${{ secrets.DOCKERHUB_USERNAME }}"
    )
    assert cleanup["env"]["DOCKERHUB_DELETE_TOKEN"] == (  # nosec B101
        "${{ !inputs.dry_run && secrets.DOCKERHUB_DELETE_TOKEN || '' }}"
    )
    assert "registry preflight" in cleanup["run"]  # nosec B101
    assert "--repo" in cleanup["run"]  # nosec B101
    assert "--component" in cleanup["run"]  # nosec B101
    assert "--check-delete-scope" in cleanup["run"]  # nosec B101
    assert "registry delete-dockerhub-tags" in cleanup["run"]  # nosec B101
    assert "dockerhub_image is dry-run only" in cleanup["run"]  # nosec B101
    assert "--required-substring" in cleanup["run"]  # nosec B101
    assert "--required-substring alpha" in cleanup["run"]  # nosec B101


def test_app_code_checkouts_do_not_persist_credentials() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in (
        "control-plane",
        "poll-checks",
        "manual-registry-publish",
        "poll-registry-publish",
    ):
        job = workflow["jobs"][job_name]
        checkout = _step(job, "Checkout app repo")

        assert checkout["with"]["persist-credentials"] is False  # nosec B101


def test_app_code_checkouts_keep_manual_and_publish_pr_submodule_guards() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual = _step(workflow["jobs"]["control-plane"], "Checkout app repo")
    poll = _step(workflow["jobs"]["poll-checks"], "Checkout app repo")
    manual_trusted = _step(
        workflow["jobs"]["control-plane"], "Initialize trusted PR submodules"
    )
    poll_trusted = _step(
        workflow["jobs"]["poll-checks"], "Initialize trusted PR submodules"
    )
    manual_publish = _step(
        workflow["jobs"]["manual-registry-publish"], "Checkout app repo"
    )
    poll_publish = _step(workflow["jobs"]["poll-registry-publish"], "Checkout app repo")

    assert "checkout_submodules" in manual["with"]["submodules"]  # nosec B101
    assert (
        "inputs.event != 'pull_request'" in manual["with"]["submodules"]
    )  # nosec B101
    assert "checkout_submodules" in poll["with"]["submodules"]  # nosec B101
    assert (
        "matrix.target.event != 'pull_request'" in poll["with"]["submodules"]
    )  # nosec B101
    assert "inputs.event == 'pull_request'" in manual_trusted["if"]  # nosec B101
    assert "matrix.target.event == 'pull_request'" in poll_trusted["if"]  # nosec B101
    assert "gitmodules" in manual_trusted["run"]  # nosec B101
    assert 'record.partition("\\n")' in manual_trusted["run"]  # nosec B101
    assert "range(0, len(matches) - 1, 2)" not in manual_trusted["run"]  # nosec B101
    assert "submodule.{section_name}.url" in manual_trusted["run"]  # nosec B101
    assert 'record.partition("\\n")' in poll_trusted["run"]  # nosec B101
    assert "range(0, len(matches) - 1, 2)" not in poll_trusted["run"]  # nosec B101
    assert "submodule.{section_name}.url" in poll_trusted["run"]  # nosec B101
    assert "untrusted submodule url" in poll_trusted["run"]  # nosec B101
    assert "checkout_submodules" in manual_publish["with"]["submodules"]  # nosec B101
    assert (
        "inputs.event != 'pull_request'" in manual_publish["with"]["submodules"]
    )  # nosec B101
    assert "checkout_submodules" in poll_publish["with"]["submodules"]  # nosec B101
    assert (
        "matrix.target.event != 'pull_request'" in poll_publish["with"]["submodules"]
    )  # nosec B101


def test_control_check_steps_gate_publish_explicitly() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual = _step(workflow["jobs"]["control-plane"], "Run central control check")
    manual_publish_job = workflow["jobs"]["manual-registry-publish"]
    manual_publish = _step(manual_publish_job, "Publish registry images")
    manual_preflight = _step(manual_publish_job, "Validate publish credentials")
    poll = _step(workflow["jobs"]["poll-checks"], "Run central control check")
    poll_publish_job = workflow["jobs"]["poll-registry-publish"]
    poll_publish = _step(poll_publish_job, "Publish registry images")
    poll_preflight = _step(poll_publish_job, "Validate publish credentials")

    assert "registry preflight --mode publish" in manual_preflight["run"]  # nosec B101
    assert 'if [[ "${PUBLISH}" == "true" ]]' in manual["run"]  # nosec B101
    assert (  # nosec B101
        "args+=(--publish --validation-only --no-github-prereleases)" in manual["run"]
    )
    assert (
        manual_publish["uses"] == "./.github/actions/publish-registry-images"
    )  # nosec B101
    assert (
        "steps.registry-preflight.outcome == 'success'" in manual_publish["if"]
    )  # nosec B101
    assert manual_publish["with"]["publish-component"] == (  # nosec B101
        "${{ inputs.publish_component }}"
    )
    assert REGISTRY_PUBLISH_ENV_KEYS.issubset(manual_publish["env"])  # nosec B101
    assert not REGISTRY_PUBLISH_SECRET_ENV_KEYS.intersection(
        manual_publish["env"]
    )  # nosec B101
    assert not REGISTRY_PUBLISH_ENV_KEYS.intersection(manual["env"])  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" not in manual["env"]  # nosec B101
    assert "--report-json" in manual["run"]  # nosec B101
    assert "registry preflight --mode publish" in poll_preflight["run"]  # nosec B101
    assert 'if [[ "${TARGET_PUBLISH}" == "true" ]]' in poll["run"]  # nosec B101
    assert (  # nosec B101
        "args+=(--publish --validation-only --no-github-prereleases)" in poll["run"]
    )
    assert (
        poll_publish["uses"] == "./.github/actions/publish-registry-images"
    )  # nosec B101
    assert (
        "steps.registry-preflight.outcome == 'success'" in poll_publish["if"]
    )  # nosec B101
    assert poll_publish["with"]["publish-components-json"] == (  # nosec B101
        "${{ toJSON(matrix.target.publish_components) }}"
    )
    assert REGISTRY_PUBLISH_ENV_KEYS.issubset(poll_publish["env"])  # nosec B101
    assert not REGISTRY_PUBLISH_SECRET_ENV_KEYS.intersection(
        poll_publish["env"]
    )  # nosec B101
    assert not REGISTRY_PUBLISH_ENV_KEYS.intersection(poll["env"])  # nosec B101
    assert "--publish-only" in PUBLISH_ACTION.read_text()  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" not in poll["env"]  # nosec B101
    assert "--report-json" in poll["run"]  # nosec B101
    assert "args+=(--no-integration)" not in poll["run"]  # nosec B101


def test_registry_publish_action_validates_component_json_array() -> None:
    text = PUBLISH_ACTION.read_text()

    assert "components = json.loads(value)" in text  # nosec B101
    assert "isinstance(components, list)" in text  # nosec B101
    assert "publish-components-json must be a JSON array" in text  # nosec B101


def test_registry_publish_uses_protected_environment_and_github_token() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    permissions = workflow["permissions"]
    manual_job = workflow["jobs"]["manual-registry-publish"]
    poll_job = workflow["jobs"]["poll-registry-publish"]
    manual_preflight = _step(manual_job, "Validate publish credentials")
    manual_publish = _step(manual_job, "Publish registry images")
    manual_dockerhub_login = _step(manual_job, "Log in to Docker Hub")
    manual_ghcr_login = _step(manual_job, "Log in to GHCR")
    poll_preflight = _step(poll_job, "Validate publish credentials")
    poll_publish = _step(poll_job, "Publish registry images")
    poll_dockerhub_login = _step(poll_job, "Log in to Docker Hub")
    poll_ghcr_login = _step(poll_job, "Log in to GHCR")

    assert "packages" not in permissions  # nosec B101
    assert manual_job["permissions"]["packages"] == "write"  # nosec B101
    assert poll_job["permissions"]["packages"] == "write"  # nosec B101
    assert manual_job["environment"]["name"] == "registry-publish"  # nosec B101
    assert poll_job["environment"]["name"] == "registry-publish"  # nosec B101
    assert "github.run_id" in manual_job["environment"]["url"]  # nosec B101
    assert "github.run_id" in poll_job["environment"]["url"]  # nosec B101
    assert (
        "needs.control-plane.outputs.manual_control_check_outcome == 'success'"
        in manual_job["if"]
    )  # nosec B101
    assert (
        "needs.control-plane.outputs.poll_has_publish_targets == 'true'"
        in poll_job["if"]
    )  # nosec B101
    assert "needs.poll-checks.result == 'success'" in poll_job["if"]  # nosec B101
    assert (
        _step(poll_job, "Validate app control report").get(  # nosec B101
            "continue-on-error"
        )
        is True
    )

    for step in (manual_preflight, manual_publish, poll_preflight, poll_publish):
        assert step["env"]["AIO_FLEET_REGISTRY_AUTH_MODE"] == (  # nosec B101
            "preauthenticated"
        )
        assert "DOCKERHUB_TOKEN" not in step["env"]  # nosec B101
        assert "AIO_FLEET_GHCR_TOKEN" not in step["env"]  # nosec B101

    for step in (
        manual_dockerhub_login,
        manual_ghcr_login,
        poll_dockerhub_login,
        poll_ghcr_login,
    ):
        assert step.get("continue-on-error") is True  # nosec B101
        assert step["env"]["DOCKER_CONFIG"] == (  # nosec B101
            "${{ runner.temp }}/aio-fleet-docker-config"
        )
        assert "--password-stdin" in step["run"]  # nosec B101

    for step in (manual_dockerhub_login, poll_dockerhub_login):
        assert step["env"]["DOCKERHUB_PUBLISH_TOKEN"] == (  # nosec B101
            "${{ secrets.DOCKERHUB_PUBLISH_TOKEN }}"
        )
        assert "docker login docker.io" in step["run"]  # nosec B101

    for step in (manual_ghcr_login, poll_ghcr_login):
        assert step["env"]["GHCR_TOKEN"] == ("${{ github.token }}")  # nosec B101
        assert "docker login ghcr.io" in step["run"]  # nosec B101

    for job in (manual_job, poll_job):
        setup = _step(job, "Set up Python")
        assert "cache" not in setup["with"]  # nosec B101


def test_registry_publish_approval_context_is_visible() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    control = workflow["jobs"]["control-plane"]
    manual_job = workflow["jobs"]["manual-registry-publish"]
    poll_job = workflow["jobs"]["poll-registry-publish"]
    summary = _step(control, "Summarize registry publish approval")

    assert "inputs.repo" in manual_job["name"]  # nosec B101
    assert "inputs.publish_component" in manual_job["name"]  # nosec B101
    assert "inputs.sha" in manual_job["name"]  # nosec B101
    assert "matrix.target.repo" in poll_job["name"]  # nosec B101
    assert "matrix.target.publish_components" in poll_job["name"]  # nosec B101
    assert "matrix.target.sha" in poll_job["name"]  # nosec B101
    assert "inputs.publish" in summary["if"]  # nosec B101
    assert "GITHUB_STEP_SUMMARY" in summary["run"]  # nosec B101
    assert "TARGET_REPO" in summary["env"]  # nosec B101
    assert "PUBLISH_COMPONENT" in summary["env"]  # nosec B101
    assert "TARGET_SHA" in summary["env"]  # nosec B101


def test_publish_preflight_runs_before_docker_setup_and_writes_artifact() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("manual-registry-publish", "poll-registry-publish"):
        job = workflow["jobs"][job_name]
        names = [step["name"] for step in job["steps"]]
        preflight = _step(job, "Validate publish credentials")

        assert names.index("Validate publish credentials") < names.index(  # nosec B101
            "Set up QEMU"
        )
        assert names.index("Validate publish credentials") < names.index(  # nosec B101
            "Set up Docker Buildx"
        )
        assert "registry-preflight-report.json" in preflight["run"]  # nosec B101
        assert "workflow control-report" in preflight["run"]  # nosec B101
        assert (
            "credential-gap: registry publish preflight failed" in preflight["run"]
        )  # nosec B101
        assert "CONTROL_REPORT" in preflight["env"]  # nosec B101


def test_bootstrap_check_failures_write_structured_control_report() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual = _step(workflow["jobs"]["control-plane"], "Start central control check")
    manual_checkout = _step(workflow["jobs"]["control-plane"], "Checkout app repo")
    manual_complete = _step(
        workflow["jobs"]["control-plane"], "Complete central control check"
    )
    poll = _step(workflow["jobs"]["poll-checks"], "Start central control check")
    poll_checkout = _step(workflow["jobs"]["poll-checks"], "Checkout app repo")
    poll_complete = _step(
        workflow["jobs"]["poll-checks"], "Complete central control check"
    )

    for step in (manual, poll):
        assert step.get("continue-on-error") is True  # nosec B101
        assert "workflow control-report" in step["run"]  # nosec B101
        assert (
            "app-check-permission: bootstrap check-run failed" in step["run"]
        )  # nosec B101
        assert "--output" in step["run"]  # nosec B101

    assert (
        "start-central-control-check.outcome == 'success'" in manual_checkout["if"]
    )  # nosec B101
    assert (
        "start-poll-central-control-check.outcome == 'success'" in poll_checkout["if"]
    )  # nosec B101
    assert (
        "start-central-control-check.outcome == 'success'" in manual_complete["if"]
    )  # nosec B101
    assert (
        "start-poll-central-control-check.outcome == 'success'" in poll_complete["if"]
    )  # nosec B101


def test_poll_fast_cleanup_skips_checkout_and_full_validation() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    poll = workflow["jobs"]["poll-checks"]

    validate = _step(poll, "Validate fast cleanup scope")
    trunk = _step(poll, "Install Trunk")
    checkout = _step(poll, "Checkout app repo")
    run_check = _step(poll, "Run central control check")
    restore = _step(poll, "Restore trusted aio-fleet checkout")
    complete = _step(poll, "Complete central control check")

    assert validate.get("continue-on-error") is True  # nosec B101
    assert "control-check" in validate["run"]  # nosec B101
    assert "--fast-path-only" in validate["run"]  # nosec B101
    assert "--resolve-changed-files" in validate["run"]  # nosec B101
    assert "--changed-files-json" not in validate["run"]  # nosec B101
    assert "--check-run" not in validate["run"]  # nosec B101
    assert "AIO_FLEET_CHECK_TOKEN" not in validate.get("env", {})  # nosec B101
    assert "GH_TOKEN" in validate.get("env", {})  # nosec B101
    assert "steps.fast-cleanup-scope.outcome != 'success'" in trunk["if"]  # nosec B101
    assert (
        "steps.fast-cleanup-scope.outcome != 'success'" in checkout["if"]
    )  # nosec B101
    assert (
        "steps.fast-cleanup-scope.outcome != 'success'" in run_check["if"]
    )  # nosec B101
    assert "--fast-path-only" not in run_check["run"]  # nosec B101
    assert (
        "steps.fast-cleanup-scope.outcome != 'success'" in restore["if"]
    )  # nosec B101
    assert (
        "matrix.target.check_mode != 'fast-cleanup'" not in complete["if"]
    )  # nosec B101
    assert (
        "steps.fast-cleanup-scope.outcome == 'success'" in complete["if"]
    )  # nosec B101
    assert "aio-fleet cleanup-only fast path passed" in complete["run"]  # nosec B101


def test_app_token_resolution_prefers_app_client_id() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("control-plane", "poll-checks"):
        token_step = _step(workflow["jobs"][job_name], "Resolve GitHub App token")

        assert token_step["env"]["AIO_FLEET_APP_CLIENT_ID"] == (  # nosec B101
            "${{ vars.AIO_FLEET_APP_CLIENT_ID }}"
        )
        assert token_step["env"]["AIO_FLEET_APP_ID"] == (  # nosec B101
            "${{ secrets.AIO_FLEET_APP_ID }}"
        )
        assert "python -m aio_fleet.github_app" in token_step["run"]  # nosec B101


def test_control_plane_uploads_release_dashboard_and_preflight_artifacts() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    control = _step(workflow["jobs"]["control-plane"], "Upload control-plane artifacts")
    poll = _step(workflow["jobs"]["poll-checks"], "Upload poll-check artifacts")
    manual_publish = _step(
        workflow["jobs"]["manual-registry-publish"], "Upload manual publish artifacts"
    )
    poll_publish = _step(
        workflow["jobs"]["poll-registry-publish"], "Upload poll publish artifacts"
    )
    release_plan = _step(
        workflow["jobs"]["control-plane"], "Generate release plan report"
    )
    transaction_queue = _step(
        workflow["jobs"]["control-plane"], "Generate release transaction queue report"
    )

    assert "actions/upload-artifact@" in control["uses"]  # nosec B101
    assert "actions/upload-artifact@" in poll["uses"]  # nosec B101
    assert "release-plan-report.json" in release_plan["run"]  # nosec B101
    assert (
        "release plan --all --registry --registry-verify-attempts 1 --format json"
        in release_plan["run"]
    )  # nosec B101
    assert "PIPESTATUS[0]" in release_plan["run"]  # nosec B101
    assert "release reconcile" in transaction_queue["run"]  # nosec B101
    assert "fleet-dashboard-report.json" in control["with"]["path"]  # nosec B101
    assert "release-plan-report.json" in control["with"]["path"]  # nosec B101
    assert "release-transaction-report.json" in control["with"]["path"]  # nosec B101
    assert "registry-preflight-report.json" not in control["with"]["path"]  # nosec B101
    assert "registry-preflight-report.json" not in poll["with"]["path"]  # nosec B101
    assert (
        "registry-preflight-report.json" in manual_publish["with"]["path"]
    )  # nosec B101
    assert (
        "registry-preflight-report.json" in poll_publish["with"]["path"]
    )  # nosec B101
    assert "central-control-check.log" not in control["with"]["path"]  # nosec B101
    assert "central-control-check.log" not in poll["with"]["path"]  # nosec B101


def test_control_plane_can_reconcile_standards_drift() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    on_config = workflow.get("on", workflow.get(True))
    mode = on_config["workflow_dispatch"]["inputs"]["mode"]
    reconcile = _step(workflow["jobs"]["control-plane"], "Reconcile standards drift")
    upload = _step(workflow["jobs"]["control-plane"], "Upload control-plane artifacts")

    assert "standards-reconcile" in mode["options"]  # nosec B101
    assert "standards reconcile" in reconcile["run"]  # nosec B101
    assert "--github" in reconcile["run"]  # nosec B101
    assert "--release" in reconcile["run"]  # nosec B101
    assert "--allow-drift" in reconcile["run"]  # nosec B101
    assert "fleet-dashboard.manifest.yml" in reconcile["run"]  # nosec B101
    assert "standards-reconcile-report.json" in reconcile["run"]  # nosec B101
    assert "actions" in reconcile["run"]  # nosec B101
    assert "GH_TOKEN" in reconcile["env"]  # nosec B101
    assert "standards-reconcile-report.json" in upload["with"]["path"]  # nosec B101


def test_github_prerelease_token_is_scoped_to_trusted_publish_step() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual_run = _step(workflow["jobs"]["control-plane"], "Run central control check")
    manual_release = _step(
        workflow["jobs"]["manual-registry-publish"], "Publish GitHub prereleases"
    )
    poll_run = _step(workflow["jobs"]["poll-checks"], "Run central control check")
    poll_release = _step(
        workflow["jobs"]["poll-registry-publish"], "Publish GitHub prereleases"
    )

    assert "AIO_FLEET_RELEASE_TOKEN" not in manual_run.get("env", {})  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" not in poll_run.get("env", {})  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" in manual_release["env"]  # nosec B101
    assert "AIO_FLEET_RELEASE_TOKEN" in poll_release["env"]  # nosec B101
    assert (
        "steps.registry-publish.outcome == 'success'" in manual_release["if"]
    )  # nosec B101
    assert (
        "steps.poll-registry-publish.outcome == 'success'" in poll_release["if"]
    )  # nosec B101
    assert "publish-github-prereleases" in manual_release["run"]  # nosec B101
    assert "publish-github-prereleases" in poll_release["run"]  # nosec B101
    assert "--expected-sha" in manual_release["run"]  # nosec B101
    assert "--expected-sha" in poll_release["run"]  # nosec B101
    assert manual_release["env"]["TARGET_SHA"] == "${{ inputs.sha }}"  # nosec B101
    assert poll_release["env"]["TARGET_SHA"] == "${{ matrix.target.sha }}"  # nosec B101


def test_publish_workflow_concurrency_is_component_and_sha_scoped() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    top_level = workflow["concurrency"]
    poll = workflow["jobs"]["poll-checks"]["concurrency"]
    poll_publish = workflow["jobs"]["poll-registry-publish"]["concurrency"]

    assert top_level["cancel-in-progress"] is False  # nosec B101
    assert "inputs.sha" in top_level["group"]  # nosec B101
    assert "inputs.publish_component" in top_level["group"]  # nosec B101
    assert poll["cancel-in-progress"] is False  # nosec B101
    assert "aio-fleet-check" in poll["group"]  # nosec B101
    assert "matrix.target.repo" in poll["group"]  # nosec B101
    assert "matrix.target.source" in poll["group"]  # nosec B101
    assert "matrix.target.sha" in poll["group"]  # nosec B101
    assert "publish_components" not in poll["group"]  # nosec B101
    assert poll_publish["cancel-in-progress"] is False  # nosec B101
    assert "aio-fleet-publish" in poll_publish["group"]  # nosec B101
    assert "matrix.target.repo" in poll_publish["group"]  # nosec B101
    assert "matrix.target.sha" in poll_publish["group"]  # nosec B101
    assert "matrix.target.publish_components" in poll_publish["group"]  # nosec B101


def test_publish_alert_steps_use_report_json_and_alert_secret() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual = _step(
        workflow["jobs"]["manual-registry-publish"], "Alert central publish status"
    )
    poll = _step(
        workflow["jobs"]["poll-registry-publish"], "Alert poll-check publish status"
    )

    for step in (manual, poll):
        assert step["env"]["AIO_FLEET_ALERT_WEBHOOK_URL"] == (  # nosec B101
            "${{ secrets.AIO_FLEET_ALERT_WEBHOOK_URL }}"
        )
        assert "--event publish" in step["run"]  # nosec B101
        assert "--report-json" in step["run"]  # nosec B101
        assert "--details-url" in step["run"]  # nosec B101


def test_registry_credentials_are_not_logged_in_before_app_checks() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("control-plane", "poll-checks"):
        job = workflow["jobs"][job_name]
        step_names = [step["name"] for step in job["steps"]]
        run_check = _step(job, "Run central control check")

        assert "Login to Docker Hub" not in step_names  # nosec B101
        assert "Login to GHCR" not in step_names  # nosec B101
        assert "Publish registry images" not in step_names  # nosec B101
        assert not REGISTRY_PUBLISH_ENV_KEYS.intersection(
            run_check["env"]
        )  # nosec B101

    for job_name in ("manual-registry-publish", "poll-registry-publish"):
        publish = _step(workflow["jobs"][job_name], "Publish registry images")
        assert REGISTRY_PUBLISH_ENV_KEYS.issubset(set(publish["env"]))  # nosec B101


def test_trunk_setup_actions_do_not_receive_job_scoped_secrets() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("control-plane", "poll-checks"):
        job = workflow["jobs"][job_name]
        trunk_setup = _step(job, "Install Trunk")

        assert not SECRET_ENV_KEYS.intersection(job.get("env", {}))  # nosec B101
        assert "env" not in trunk_setup  # nosec B101


def test_workflow_installs_central_dependencies_before_app_checks() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("control-plane", "poll-checks"):
        job = workflow["jobs"][job_name]
        install = _step(job, "Install aio-fleet")
        run_check = _step(job, "Run central control check")

        assert 'python -m pip install -e ".[dev]"' in install["run"]  # nosec B101
        assert "python -m aio_fleet" in run_check["run"]  # nosec B101
        assert "control-check" in run_check["run"]  # nosec B101


def test_workflow_forwards_component_scoped_publish() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    trigger = workflow.get("on") or workflow.get(True)
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert "publish_component" in inputs  # nosec B101

    manual_run = _step(workflow["jobs"]["control-plane"], "Run central control check")
    poll_run = _step(workflow["jobs"]["poll-checks"], "Run central control check")

    assert "PUBLISH_COMPONENT" in manual_run["env"]  # nosec B101
    assert "--publish-component" in manual_run["run"]  # nosec B101
    assert "TARGET_PUBLISH_COMPONENTS" in poll_run["env"]  # nosec B101
    assert "--publish-component" in poll_run["run"]  # nosec B101


def test_poll_failure_alert_uses_report_context() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    poll = workflow["jobs"]["poll-checks"]
    failure = _step(poll, "Alert poll-check failure")
    run = failure["run"]

    assert "matrix.target.source" in poll["name"]  # nosec B101
    assert "TARGET_SOURCE" in failure["env"]  # nosec B101
    assert "TARGET_SHA" in failure["env"]  # nosec B101
    assert "CONTROL_REPORT" in failure["env"]  # nosec B101
    assert "--report-json" in run  # nosec B101
    assert (  # nosec B101
        "poll-check:${TARGET_REPO}:${TARGET_SOURCE}:${TARGET_SHA}" in run
    )


def test_privileged_completion_restores_trusted_checkout_first() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    for job_name in ("control-plane", "poll-checks"):
        job = workflow["jobs"][job_name]
        restore = _step(job, "Restore trusted aio-fleet checkout")
        complete = _step(job, "Complete central control check")

        assert "AIO_FLEET_CHECK_TOKEN" not in restore.get("env", {})  # nosec B101
        assert "git reset --hard HEAD" in restore["run"]  # nosec B101
        assert "git clean -ffd -e app-repo/" in restore["run"]  # nosec B101
        assert (
            "python -m pip install --force-reinstall ." in restore["run"]
        )  # nosec B101
        assert "python -I -m aio_fleet check run" in complete["run"]  # nosec B101

    for job_name in ("manual-registry-publish", "poll-registry-publish"):
        complete = _step(workflow["jobs"][job_name], "Complete central control check")
        assert "PUBLISH_OUTCOME" in complete["env"]  # nosec B101
        assert "RELEASE_OUTCOME" in complete["env"]  # nosec B101
        assert "registry publish failed" in complete["run"]  # nosec B101
        assert "GitHub prerelease publish failed" in complete["run"]  # nosec B101


def test_prerelease_publish_resets_app_checkout_to_reviewed_sha() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())

    manual_job = workflow["jobs"]["manual-registry-publish"]
    poll_job = workflow["jobs"]["poll-registry-publish"]

    manual_reset = _step(manual_job, "Reset app checkout before prerelease publish")
    manual_release = _step(manual_job, "Publish GitHub prereleases")
    poll_reset = _step(poll_job, "Reset app checkout before prerelease publish")
    poll_release = _step(poll_job, "Publish GitHub prereleases")

    assert (
        "steps.registry-preflight.outcome == 'success'" in manual_reset["if"]
    )  # nosec B101
    assert (
        "steps.registry-preflight.outcome == 'success'" in poll_reset["if"]
    )  # nosec B101
    assert (
        'git -C app-repo reset --hard "${TARGET_SHA}"' in manual_reset["run"]
    )  # nosec B101
    assert "git -C app-repo clean -ffd" in manual_reset["run"]  # nosec B101
    assert (
        'git -C app-repo reset --hard "${TARGET_SHA}"' in poll_reset["run"]
    )  # nosec B101
    assert "git -C app-repo clean -ffd" in poll_reset["run"]  # nosec B101
    assert "--repo-path app-repo" in manual_release["run"]  # nosec B101
    assert "--repo-path app-repo" in poll_release["run"]  # nosec B101
    assert '--expected-sha "${TARGET_SHA}"' in manual_release["run"]  # nosec B101
    assert '--expected-sha "${TARGET_SHA}"' in poll_release["run"]  # nosec B101


def test_dashboard_update_receives_alert_env_without_app_check_leakage() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    dashboard = _step(workflow["jobs"]["control-plane"], "Update fleet dashboard issue")
    status_alert = _step(
        workflow["jobs"]["control-plane"], "Alert control-plane status"
    )
    dashboard_env = dashboard["env"]

    assert (  # nosec B101
        dashboard_env["AIO_FLEET_KUMA_PUSH_URL"]
        == "${{ secrets.AIO_FLEET_KUMA_PUSH_URL }}"
    )
    assert (  # nosec B101
        dashboard_env["AIO_FLEET_ALERT_WEBHOOK_URL"]
        == "${{ secrets.AIO_FLEET_ALERT_WEBHOOK_URL }}"
    )
    assert "fleet-dashboard.err" in dashboard["run"]  # nosec B101
    assert "--failure-file fleet-dashboard.err" in status_alert["run"]  # nosec B101

    manual_run = _step(workflow["jobs"]["control-plane"], "Run central control check")
    poll_run = _step(workflow["jobs"]["poll-checks"], "Run central control check")
    assert "AIO_FLEET_ALERT_WEBHOOK_URL" not in manual_run.get("env", {})  # nosec B101
    assert "AIO_FLEET_ALERT_WEBHOOK_URL" not in poll_run.get("env", {})  # nosec B101


def test_catalog_changelog_mode_uses_central_generator_and_signed_pr() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["control-plane"]
    modes = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"][
        "mode"
    ]["options"]
    checkout = _step(job, "Checkout catalog for changelog")
    generate = _step(job, "Generate catalog changelog")
    create_pr = _step(job, "Create catalog changelog PR")

    assert "catalog-changelog" in modes  # nosec B101
    assert "wgross19/awesome-unraid" == checkout["with"]["repository"]  # nosec B101
    assert "steps.app-token.outputs.token" in checkout["with"]["token"]  # nosec B101
    assert "catalog-changelog" in generate["run"]  # nosec B101
    assert "--catalog-path" in generate["run"]  # nosec B101
    assert "--write" in generate["run"]  # nosec B101
    assert "status --porcelain -- CHANGELOG.md" in generate["run"]  # nosec B101
    assert "peter-evans/create-pull-request@" in create_pr["uses"]  # nosec B101
    assert create_pr["with"]["sign-commits"] is True  # nosec B101
    assert create_pr["with"]["add-paths"] == "CHANGELOG.md"  # nosec B101


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"missing workflow step: {name}")
