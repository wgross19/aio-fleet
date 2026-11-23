"""Unit fixtures and live-evidence tests for the honcho native release gate.

Everything here is dockerless and pytest-local. The mutation cases build
clearly isolated fixtures in ``tmp_path``; the actual-output cases parse the
real receipts and the real pytest JUnit report from the manager's final local
AMD64 run (p3-run-UODxnlkh) and require the gate to reject an ARM-hosted
presentation of that AMD64-local evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aio_fleet import honcho_native_check as gate
from aio_fleet.honcho_native_check import (
    HonchoGateError,
    load_receipts,
    validate_junit,
    validate_receipts,
)

AMD64_PREVIOUS = "dub19/honcho-aio@sha256:7a532cdc01e2d34cb52589fb64e3c784f8d8c979d027e81e4f1528c48a64511b"
AMD64_PREVIOUS_ID = "sha256:7a532cdc01e2d34cb52589fb64e3c784f8d8c979d027e81e4f1528c48a64511b"
AMD64_CANDIDATE = "127.0.0.1:32773/honcho-p3@sha256:e13daea7e22ff4661061b6992733fb2f243113cf23e359315f769962188f95e3"
AMD64_CANDIDATE_ID = "sha256:e13daea7e22ff4661061b6992733fb2f243113cf23e359315f769962188f95e3"

ARM64_PREVIOUS = AMD64_PREVIOUS
ARM64_CANDIDATE = "127.0.0.1:5000/honcho-arm64-111-1@sha256:" + "ab" * 32
ARM64_CANDIDATE_ID = "sha256:" + "cd" * 32
ARM64_CANDIDATE_ID_ALT = "sha256:" + "ef" * 32

# Portable excerpt from p3-run-UODxnlkh. JUnit retains case names and counts,
# not host-specific paths or timestamps. Never skip these checks on hosted CI.
ACTUAL_AMD64_RUN = Path(__file__).parent / "fixtures" / "honcho-native-amd64"

# Real run tokens are 12 lowercase hex (verified on the actual AMD64 receipts).
TOKEN_A = "aabb00cc11dd"
TOKEN_B = "1122ee33ff44"
SUITE_SHA = "ab" * 20


def _contents(scenario: str, token: str) -> dict:
    return {
        "seed": f"release-quality-{scenario}-seed-{token}",
        "after_replace": f"release-quality-{scenario}-replace-{token}",
        "after_restart": f"release-quality-{scenario}-restart-{token}",
    }


def _receipt(
    scenario: str,
    *,
    mode: str,
    arch: str,
    candidate_ref: str,
    candidate_id: str,
    token: str | None = None,
    final_count: int = 3,
    message_ids: tuple[str, str, str] | None = None,
    workspace: str | None = None,
) -> dict:
    token = token or (TOKEN_A if scenario == "same_digest_smoke" else TOKEN_B)
    message_ids = message_ids or (
        f"msg-{scenario}-0", f"msg-{scenario}-1", f"msg-{scenario}-2",
    )
    return {
        "scenario": scenario,
        "suite_git_head": SUITE_SHA,
        "classification": (
            "harness-sanity-same-digest"
            if scenario == "same_digest_smoke"
            else "two-version-upgrade"
        ),
        "docker_endpoint_mode": mode,
        "arch": f"linux/{arch}/linux/{arch}",
        "run_token": token,
        "workspace": workspace or f"honcho-rq-{scenario.replace('_', '-')}-1a2b3c4d5e-ws",
        "previous_ref": AMD64_PREVIOUS,
        "previous_image_id": AMD64_PREVIOUS_ID if candidate_id else candidate_id,
        "candidate_ref": candidate_ref,
        "candidate_image_id": candidate_id,
        "message_id": message_ids[0],
        "write_after_replace_id": message_ids[1],
        "write_after_restart_id": message_ids[2],
        "contents": _contents(scenario, token),
        "disk_message_counts": {
            "disk_before_seed": 0,
            "disk_after_seed": 1,
            "disk_after_replace": 1,
            "disk_after_write_on_candidate": 2,
            "disk_after_restart": 2,
            "disk_after_post_restart_write": final_count,
        },
    }


def _pair(mode: str, arch: str, candidate_ref: str, candidate_id: str) -> list[dict]:
    smoke = _receipt(
        "same_digest_smoke", mode=mode, arch=arch,
        candidate_ref=AMD64_PREVIOUS, candidate_id=AMD64_PREVIOUS_ID,
    )
    upgrade = _receipt(
        "two_version_upgrade", mode=mode, arch=arch,
        candidate_ref=candidate_ref, candidate_id=candidate_id,
    )
    return [smoke, upgrade]


def _write_pair(tmp_path: Path, **kwargs) -> Path:
    receipts = _pair(
        kwargs.pop("mode"), kwargs.pop("arch"),
        kwargs.pop("candidate_ref"), kwargs.pop("candidate_id"),
    )
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    for receipt in receipts:
        (receipts_dir / f"honcho-aio-{receipt['scenario']}-{receipt['run_token']}.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
    if kwargs:
        raise TypeError(f"unused kwargs: {sorted(kwargs)}")
    return receipts_dir


def _amd64_call(tmp_path: Path, **overrides):
    receipts_dir = _write_pair(
        tmp_path, mode="dind-tls", arch="amd64",
        candidate_ref=AMD64_CANDIDATE, candidate_id=AMD64_CANDIDATE_ID,
        **overrides,
    )
    return validate_receipts(
        load_receipts(receipts_dir),
        expected_suite_sha=SUITE_SHA,
        expected_mode="dind-tls",
        expected_arch="amd64",
        expected_previous_image=AMD64_PREVIOUS,
        expected_previous_id=AMD64_PREVIOUS_ID,
        expected_candidate_image=AMD64_CANDIDATE,
        expected_candidate_id=AMD64_CANDIDATE_ID,
    )


def _arm64_call(tmp_path: Path, **overrides):
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
        **overrides,
    )
    return validate_receipts(
        load_receipts(receipts_dir),
        expected_suite_sha=SUITE_SHA,
        expected_mode="host-default",
        expected_arch="arm64",
        expected_previous_image=ARM64_PREVIOUS,
        expected_previous_id=AMD64_PREVIOUS_ID,
        expected_candidate_image=ARM64_CANDIDATE,
        expected_candidate_id=ARM64_CANDIDATE_ID,
    )


# ── Isolated fixture: both approved presentations validate ────────────────


def test_both_approved_presentations_validate(tmp_path: Path) -> None:
    (tmp_path / "amd").mkdir()
    (tmp_path / "arm").mkdir()
    amd = _amd64_call(tmp_path / "amd")
    assert amd["same_image_control"] == "bound"
    assert amd["final_disk_messages"] == 3
    arm = _arm64_call(tmp_path / "arm")
    assert arm["mode"] == "host-default"
    assert sorted(arm["scenarios"]) == ["same_digest_smoke", "two_version_upgrade"]


def test_arm_mismatch_rejected_on_actual_amd64_evidence(tmp_path: Path) -> None:
    """AMD64-local receipts presented as hosted-ARM64 evidence must be refused."""
    receipts_dir = _write_pair(
        tmp_path, mode="dind-tls", arch="amd64",
        candidate_ref=AMD64_CANDIDATE, candidate_id=AMD64_CANDIDATE_ID,
    )
    with pytest.raises(HonchoGateError, match="mode/arch mismatch|expected mode"):
        validate_receipts(
            load_receipts(receipts_dir),
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default",
            expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


# ── Bindings: baseline, staged candidate, control, count chain ─────────────


def test_baseline_must_be_the_reviewed_repository(tmp_path: Path) -> None:
    with pytest.raises(HonchoGateError, match="reviewed baseline repository"):
        validate_receipts(
            load_receipts(_write_pair(
                tmp_path, mode="host-default", arch="arm64",
                candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
            )),
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image="evil/honcho-aio@sha256:" + "0" * 64,
            expected_previous_id="sha256:" + "0" * 64,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_candidate_must_be_loopback_staged(tmp_path: Path) -> None:
    with pytest.raises(HonchoGateError, match="loopback-staged"):
        validate_receipts(
            load_receipts(_write_pair(
                tmp_path, mode="host-default", arch="arm64",
                candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
            )),
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image="ghcr.io/owner/honcho@sha256:" + "1" * 64,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_same_image_control_bound_to_baseline(tmp_path: Path) -> None:
    """The control arm must prove candidate == previous == pinned baseline."""
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        if receipt["scenario"] == "same_digest_smoke":
            receipt["candidate_image_id"] = ARM64_CANDIDATE_ID_ALT
            receipt["candidate_ref"] = ARM64_CANDIDATE
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for receipt in receipts:
        (tampered / f"{receipt['scenario']}.json").write_text(json.dumps(receipt))
    with pytest.raises(HonchoGateError, match="same-image control"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_upgrade_arm_distinct_ids_required(tmp_path: Path) -> None:
    receipts = _pair(
        "host-default", "arm64", ARM64_CANDIDATE, ARM64_CANDIDATE_ID,
    )
    for receipt in receipts:
        if receipt["scenario"] == "two_version_upgrade":
            receipt["candidate_image_id"] = AMD64_PREVIOUS_ID  # collapse
    with pytest.raises(HonchoGateError, match="collapsed to one image id"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=AMD64_PREVIOUS_ID,
        )


def test_upgrade_arm_staged_identity_mismatch_rejected(tmp_path: Path) -> None:
    receipts = _pair(
        "host-default", "arm64", ARM64_CANDIDATE, ARM64_CANDIDATE_ID,
    )
    for receipt in receipts:
        if receipt["scenario"] == "two_version_upgrade":
            receipt["candidate_image_id"] = ARM64_CANDIDATE_ID_ALT
    with pytest.raises(HonchoGateError, match="candidate id .* is not the staged"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_count_chain_regression_rejected(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt["disk_message_counts"]["disk_after_replace"] = 0  # message lost
        receipt["disk_message_counts"]["disk_after_write_on_candidate"] = 1
        receipt["disk_message_counts"]["disk_after_restart"] = 1
        receipt["disk_message_counts"]["disk_after_post_restart_write"] = 2
    with pytest.raises(HonchoGateError, match="chain broke at disk_after_replace"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_reused_message_ids_rejected(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt["write_after_restart_id"] = receipt["message_id"]  # not a new write
    with pytest.raises(HonchoGateError, match="three distinct ids"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_fixed_workspace_name_rejected(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt["workspace"] = "honcho"  # not a disposable run-scoped name
    with pytest.raises(HonchoGateError, match="disposable honcho-rq- prefix"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_scenario_set_must_be_exactly_two(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    receipts[0]["scenario"] = "other_thing"
    with pytest.raises(HonchoGateError, match="unknown scenario"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_run_tokens_unique_across_scenarios(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    reused = receipts[1]["run_token"]
    receipts[0]["run_token"] = reused
    receipts[0]["contents"] = _contents(receipts[0]["scenario"], reused)
    for receipt in receipts:
        receipt["workspace"] = receipt["workspace"].replace("1a2b3c4d5e", "9f8e7d6c5a")
    with pytest.raises(HonchoGateError, match="run tokens are reused"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_contents_must_embed_the_run_token(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt["contents"]["after_replace"] = "release-quality-after_replace-forged-token"
    with pytest.raises(HonchoGateError, match="after_replace-forged|three-stage"):
        validate_receipts(
            receipts,
            expected_suite_sha=SUITE_SHA,
            expected_mode="host-default", expected_arch="arm64",
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )



def test_suite_ref_missing_from_receipt_rejected(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt.pop("suite_git_head")
    with pytest.raises(HonchoGateError, match="suite_git_head"):
        validate_receipts(
            receipts,
            expected_mode="host-default", expected_arch="arm64",
            expected_suite_sha=SUITE_SHA,
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


def test_swapped_suite_ref_rejected(tmp_path: Path) -> None:
    receipts_dir = _write_pair(
        tmp_path, mode="host-default", arch="arm64",
        candidate_ref=ARM64_CANDIDATE, candidate_id=ARM64_CANDIDATE_ID,
    )
    receipts = load_receipts(receipts_dir)
    for receipt in receipts:
        receipt["suite_git_head"] = "0" * 40  # an unreviewed suite
    with pytest.raises(HonchoGateError, match="reviewed suite ref"):
        validate_receipts(
            receipts,
            expected_mode="host-default", expected_arch="arm64",
            expected_suite_sha=SUITE_SHA,
            expected_previous_image=ARM64_PREVIOUS,
            expected_previous_id=AMD64_PREVIOUS_ID,
            expected_candidate_image=ARM64_CANDIDATE,
            expected_candidate_id=ARM64_CANDIDATE_ID,
        )


# ── JUnit gate on real pytest reports ───────────────────────────────────────


def _write_junit(tmp_path: Path, cases: list[tuple[str, str | None]]) -> Path:
    """Build a testsuites-rooted report the way real pytest writes it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, problem in cases:
        child = f"<{problem}/>" if problem else ""
        rows.append(f'<testcase classname="tests.release.test_upgrade" name="{name}" time="0.001">{child}</testcase>')
    body = "".join(rows)
    report = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites name="pytest tests"><testsuite name="pytest" errors="0" '
        f'failures="0" skipped="0" tests="{len(cases)}" time="1.0">{body}'
        f"</testsuite></testsuites>"
    )
    path = tmp_path / "results.xml"
    path.write_text(report, encoding="utf-8")
    return path


DOCKER_CASES = [
    ("test_release_contract_rejects_missing_inputs", None),
    ("test_docker_engine_verified", None),
    ("test_same_digest_smoke", None),
    ("test_two_version_upgrade", None),
]


def test_junit_accepts_named_docker_cases(tmp_path: Path) -> None:
    result = validate_junit(_write_junit(tmp_path, DOCKER_CASES), pytest_rc=0)
    assert result["tests"] == 4
    assert result["required_cases_present"] is True


def test_junit_rejects_skips_and_missing_docker_cases(tmp_path: Path) -> None:
    skipped = DOCKER_CASES[:-1] + [("test_same_digest_smoke", "skipped")]
    with pytest.raises(HonchoGateError, match="skipped cases"):
        validate_junit(_write_junit(tmp_path / "s", skipped), pytest_rc=0)
    (tmp_path / "m").mkdir(exist_ok=True)
    with pytest.raises(HonchoGateError, match="required Docker cases absent"):
        validate_junit(
            _write_junit(
                tmp_path / "m",
                [c for c in DOCKER_CASES if c[0] != "test_two_version_upgrade"],
            ),
            pytest_rc=0,
        )


def test_junit_rejects_failures_rc_and_empty_reports(tmp_path: Path) -> None:
    (tmp_path / "f").mkdir(exist_ok=True)
    failed = DOCKER_CASES + [("extra", None)]
    failed[0] = ("test_docker_engine_verified", "failure")
    path_f = _write_junit(tmp_path / "f", failed)
    with pytest.raises(HonchoGateError, match="declared 5|failures"):
        validate_junit(path_f, pytest_rc=0)  # suite declares 0 failures; rows lie
    with pytest.raises(HonchoGateError, match="pytest exited"):
        validate_junit(_write_junit(tmp_path / "r", DOCKER_CASES), pytest_rc=2)
    (tmp_path / "e").mkdir(exist_ok=True)
    with pytest.raises(HonchoGateError, match="required Docker cases absent"):
        validate_junit(
            _write_junit(tmp_path / "e", [("t1", None), ("t2", None)]),
            pytest_rc=0,
        )


# ── The verifier against the ACTUAL final AMD64 run outputs ────────────────


class TestActualAMD64Run:
    def test_receipts_validate_on_their_own_terms(self) -> None:
        actual_refs = json.loads(
            (ACTUAL_AMD64_RUN / "receipts" / "honcho-aio-two_version_upgrade-fb986edd7c21.json")
            .read_text(encoding="utf-8")
        )
        result = validate_receipts(
            load_receipts(ACTUAL_AMD64_RUN / "receipts"),
            expected_suite_sha=actual_refs["suite_git_head"],
            expected_mode=actual_refs["docker_endpoint_mode"],
            expected_arch="amd64",
            expected_previous_image=actual_refs["previous_ref"],
            expected_previous_id=actual_refs["previous_image_id"],
            expected_candidate_image=actual_refs["candidate_ref"],
            expected_candidate_id=actual_refs["candidate_image_id"],
        )
        assert result["same_image_control"] == "bound"
        assert result["final_disk_messages"] == 3
        assert result["run_tokens"] == ["6a2596aaf0f7", "fb986edd7c21"]

    def test_arm_hosted_presentation_of_the_amd64_run_is_rejected(self) -> None:
        actual_refs = json.loads(
            (ACTUAL_AMD64_RUN / "receipts" / "honcho-aio-two_version_upgrade-fb986edd7c21.json")
            .read_text(encoding="utf-8")
        )
        with pytest.raises(HonchoGateError, match="mode/arch mismatch|expected mode"):
            validate_receipts(
                load_receipts(ACTUAL_AMD64_RUN / "receipts"),
                expected_suite_sha=actual_refs["suite_git_head"],
                expected_mode="host-default",
                expected_arch="arm64",
                expected_previous_image=actual_refs["previous_ref"],
                expected_previous_id=actual_refs["previous_image_id"],
                expected_candidate_image=actual_refs["candidate_ref"],
                expected_candidate_id=actual_refs["candidate_image_id"],
            )

    def test_real_pytest_junit_passes_the_gate(self) -> None:
        result = validate_junit(ACTUAL_AMD64_RUN / "results.xml", pytest_rc=0)
        assert result["tests"] == 45
        assert result["required_cases_present"] is True
        assert gate.REQUIRED_JUNIT_CASES[0] in result["case_names"]
