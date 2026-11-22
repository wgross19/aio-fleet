from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "gbrain-arm64-release-qualification.yml"
RAW = WORKFLOW.read_text(encoding="utf-8")
JOB_NAME = "qualify"
SUITE_STEP = "Run the exact merged release suite on native arm64"


def _workflow() -> dict:
    return yaml.safe_load(RAW)


def _job() -> dict:
    return _workflow()["jobs"][JOB_NAME]


def _step(job: dict, name: str) -> dict:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"  # nosec B101
    return matches[0]


def _names(job: dict) -> list[str]:
    return [step.get("name", "") for step in job["steps"]]


def _triggers(workflow: dict) -> dict:
    # PyYAML's YAML 1.1 loader parses the `on:` key as boolean True.
    for key in ("on", True):
        if key in workflow:
            return workflow[key]
    raise AssertionError("workflow has no trigger block")  # nosec B101


def test_dispatch_is_manual_only_and_takes_one_sha() -> None:
    triggers = _triggers(_workflow())

    assert set(triggers) == {"workflow_dispatch"}  # nosec B101
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"app_sha"}  # nosec B101
    assert inputs["app_sha"]["required"] is True  # nosec B101
    assert inputs["app_sha"]["type"] == "string"  # nosec B101
    for forbidden in ("schedule", "push:", "pull_request:", "repository_dispatch"):
        assert forbidden not in RAW  # nosec B101


def test_runs_on_native_arm64_with_read_only_permissions() -> None:
    workflow, job = _workflow(), _job()

    assert job["runs-on"] == "ubuntu-24.04-arm"  # nosec B101
    assert "strategy" not in job  # no arch matrix / emulation mix  # nosec B101
    assert "environment" not in job  # no publish environment gate  # nosec B101
    assert job["timeout-minutes"] <= 120  # nosec B101
    assert workflow["permissions"] == {"contents": "read"}  # nosec B101
    lowered = RAW.lower()
    for token in ("qemu", "platform=linux/amd64", "setup-buildx", "buildx"):
        assert token not in lowered  # nosec B101


def test_never_publishes_or_uploads_an_artifact() -> None:
    assert "${{ secrets." not in RAW  # nosec B101
    for token in (
        "upload-artifact",
        "docker/login-action",
        "skopeo",
        "ghcr.io",
        "docker.io",
        "quay.io",
        "--push",
        "create-release",
        "softprops/action-gh-release",
    ):
        assert token not in RAW.lower()  # nosec B101


def test_only_loopback_receives_the_candidate() -> None:
    stage = _step(_job(), "Stage candidate in an ephemeral loopback registry")
    run = stage["run"]

    assert "-p 127.0.0.1::5000" in run  # nosec B101
    assert 'docker run -d --name "${container}"' in run  # nosec B101
    assert "gbrain-arm64-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in run  # nosec B101
    assert 'docker push --quiet "${repo}:test"' in run  # nosec B101
    push_lines = [line.strip() for line in run.splitlines() if "docker push" in line]
    assert push_lines  # nosec B101
    assert all('"${repo}:test"' in line for line in push_lines)  # nosec B101
    assert "127.0.0.1:${address##*:}" in run  # nosec B101


def test_staging_keeps_the_registry_and_tag_for_the_evidence_steps() -> None:
    job = _job()
    names = _names(job)
    stage = _step(job, "Stage candidate in an ephemeral loopback registry")
    verify = _step(job, "Verify staged inputs resolve immediately before the suite")
    cleanup = _step(job, "Remove loopback and run-scoped resources")

    assert "trap " not in stage["run"]  # nosec B101
    assert "docker image rm" not in stage["run"]  # nosec B101
    assert "registry_container=${container}" in stage["run"]  # nosec B101
    assert "repo_tag=${repo}:test" in stage["run"]  # nosec B101
    assert names.index(stage["name"]) < names.index(verify["name"])  # nosec B101
    assert names.index(verify["name"]) < names.index(SUITE_STEP)  # nosec B101
    assert "{{.State.Running}}" in verify["run"]  # nosec B101
    assert '"${PREVIOUS_IMAGE}"' in verify["run"]  # nosec B101
    assert '"${CANDIDATE_IMAGE}"' in verify["run"]  # nosec B101
    assert '"${PREVIOUS_ID}"' in verify["run"]  # nosec B101
    assert '"${CANDIDATE_ID}"' in verify["run"]  # nosec B101
    assert (
        cleanup["env"]["REPO_TAG"] == "${{ steps.stage.outputs.repo_tag }}"
    )  # nosec B101
    assert cleanup["env"]["REGISTRY_CONTAINER"] == (  # nosec B101
        "${{ steps.stage.outputs.registry_container }}"
    )
    assert 'docker rm -fv "${REGISTRY_CONTAINER}"' in cleanup["run"]  # nosec B101
    assert 'docker image rm -f "${REPO_TAG}"' in cleanup["run"]  # nosec B101


def test_manual_guard_and_sha_validation_precede_checkouts() -> None:
    names = _names(_job())
    guard = _step(_job(), "Enforce trusted ref for manual runs")
    validate = _step(_job(), "Validate dispatched app SHA")

    assert names.index(guard["name"]) < names.index(
        "Checkout trusted fleet"
    )  # nosec B101
    assert names.index(guard["name"]) < names.index(  # nosec B101
        "Checkout app tests and build context"
    )
    assert names.index(validate["name"]) < names.index(  # nosec B101
        "Checkout app tests and build context"
    )
    assert (
        "github.event.repository.default_branch" in guard["env"]["EXPECTED_REF"]
    )  # nosec B101
    assert "GITHUB_REF" in guard["run"]  # nosec B101
    assert "0-9a-f]{40}" in validate["run"]  # nosec B101


def test_app_checkout_is_the_dispatched_merged_commit() -> None:
    job = _job()
    checkout = _step(job, "Checkout app tests and build context")
    merged = _step(job, "Require a merged app commit")

    assert checkout["with"]["repository"] == "wgross19/gbrain-aio"  # nosec B101
    assert checkout["with"]["ref"] == "${{ inputs.app_sha }}"  # nosec B101
    assert checkout["with"]["path"] == "app-repo"  # nosec B101
    assert checkout["with"]["fetch-depth"] == 0  # nosec B101
    assert checkout["with"]["persist-credentials"] is False  # nosec B101
    assert 'test "$(git rev-parse HEAD)" = "${APP_SHA}"' in merged["run"]  # nosec B101
    assert "git merge-base --is-ancestor HEAD FETCH_HEAD" in merged["run"]  # nosec B101


def test_native_arch_is_asserted_not_assumed() -> None:
    job = _job()
    native = _step(job, "Prove native arm64 runner and daemon")
    pull = _step(job, "Pull pinned previous image for arm64")
    build = _step(job, "Build the candidate natively")
    suite = _step(job, SUITE_STEP)

    assert 'test "$(uname -m)" = aarch64' in native["run"]  # nosec B101
    assert "{{.Server.Arch}}" in native["run"]  # nosec B101
    assert (
        'test "${RUNNER_ENVIRONMENT}" = "github-hosted"' in native["run"]
    )  # nosec B101
    assert "--platform linux/arm64" in pull["run"]  # nosec B101
    assert "--format '{{.Architecture}}'" in pull["run"]  # nosec B101
    assert "--platform linux/arm64" in build["run"]  # nosec B101
    assert suite["env"]["AIO_RELEASE_ARCH"] == "arm64"  # nosec B101


def test_pins_come_from_the_reviewed_app_constant() -> None:
    refs = _step(_job(), "Resolve reviewed digest pins")

    assert "aio_fleet.native_release refs app-repo" in refs["run"]  # nosec B101
    assert "dub19/gbrain-aio@" not in RAW  # nosec B101
    assert "registry@sha256:" not in RAW  # nosec B101


def test_runs_the_exact_merged_release_suite() -> None:
    suite = _step(_job(), SUITE_STEP)

    assert suite["working-directory"] == "app-repo"  # nosec B101
    assert (  # nosec B101
        'python -m pytest -q tests/release --junitxml "${RUNNER_TEMP}/release-junit.xml"'
        in suite["run"]
    )
    assert "tests/integration" not in suite["run"]  # nosec B101
    assert suite["env"]["AIO_PREVIOUS_IMAGE"] == (  # nosec B101
        "${{ steps.refs.outputs.previous }}"
    )
    assert suite["env"]["AIO_CANDIDATE_IMAGE"] == (  # nosec B101
        "${{ steps.stage.outputs.candidate }}"
    )
    assert suite["env"]["AIO_RELEASE_RECEIPTS_DIR"] == (  # nosec B101
        "${{ runner.temp }}/aio-release-receipts"
    )


def test_evidence_gates_run_even_after_a_red_suite() -> None:
    job = _job()
    names = _names(job)
    junit = _step(job, "Require named cases and zero skips")
    receipts = _step(job, "Validate both ownership receipts and write summary")
    cleanup = _step(job, "Remove loopback and run-scoped resources")

    assert names.index(SUITE_STEP) < names.index(junit["name"])  # nosec B101
    assert names.index(junit["name"]) < names.index(receipts["name"])  # nosec B101
    assert junit["if"] == "${{ always() }}"  # nosec B101
    assert receipts["if"] == "${{ always() }}"  # nosec B101
    assert cleanup["if"] == "${{ always() }}"  # nosec B101
    assert "native_release junit" in junit["run"]  # nosec B101
    assert "--pytest-rc" in junit["run"]  # nosec B101
    assert "native_release receipts" in receipts["run"]  # nosec B101
    assert receipts["env"]["EXPECTED_PREVIOUS_IMAGE"] == (  # nosec B101
        "${{ steps.refs.outputs.previous }}"
    )
    assert receipts["env"]["EXPECTED_PREVIOUS_ID"] == (  # nosec B101
        "${{ steps.previous.outputs.previous_id }}"
    )
    assert receipts["env"]["EXPECTED_CANDIDATE_IMAGE"] == (  # nosec B101
        "${{ steps.stage.outputs.candidate }}"
    )
    assert receipts["env"]["EXPECTED_CANDIDATE_ID"] == (  # nosec B101
        "${{ steps.candidate.outputs.local_id }}"
    )
    for flag in (
        "--expected-sha",
        "--expected-previous",
        "--expected-previous-id",
        "--expected-candidate",
        "--expected-candidate-id",
        "--summary",
    ):
        assert flag in receipts["run"]  # nosec B101
    assert "name=^aio-rel-" in cleanup["run"]  # nosec B101
