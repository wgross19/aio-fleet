from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aio_fleet import native_release
from aio_fleet.native_release import (
    MISSING_RESERVATION,
    REQUIRED_CASES,
    SAVED_RESERVATION,
    NativeReleaseError,
    load_receipts,
    main,
    pinned_release_refs,
    summary_markdown,
    validate_junit,
    validate_receipts,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
PREVIOUS = "dub19/gbrain-aio@sha256:" + "a" * 64
REGISTRY = "registry@sha256:" + "c" * 64
PREVIOUS_ID = "sha256:" + "1" * 64
CANDIDATE_ID = "sha256:" + "2" * 64
CANDIDATE = "127.0.0.1:32768/gbrain-arm64-123-1@sha256:" + "b" * 64


def _write_junit(
    path: Path,
    names: list[str],
    *,
    skipped: str | None = None,
    failure: str | None = None,
    declared_tests: int | None = None,
    declared_skipped: int | None = None,
) -> Path:
    suite = ET.Element(
        "testsuite",
        {
            "name": "pytest",
            "tests": str(declared_tests if declared_tests is not None else len(names)),
            "failures": "1" if failure else "0",
            "errors": "0",
            "skipped": str(
                declared_skipped
                if declared_skipped is not None
                else (1 if skipped else 0)
            ),
        },
    )
    for name in names:
        case = ET.SubElement(suite, "testcase", {"name": name, "time": "0.01"})
        if name == skipped:
            ET.SubElement(case, "skipped", {"message": "explicit skip"})
        if name == failure:
            ET.SubElement(case, "failure", {"message": "boom"})
    target = path / "release-junit.xml"
    ET.ElementTree(suite).write(target, encoding="utf-8", xml_declaration=True)
    return target


def test_junit_accepts_every_required_case(tmp_path: Path) -> None:
    names = [*REQUIRED_CASES, "test_extra_case"]
    report = _write_junit(tmp_path, names)

    result = validate_junit(report, pytest_rc=0)

    assert result["tests"] == len(names)  # nosec B101
    assert result["skipped"] == 0  # nosec B101
    assert result["failures"] == 0  # nosec B101
    assert result["required_cases_present"] is True  # nosec B101
    assert set(REQUIRED_CASES) <= set(result["case_names"])  # nosec B101


def test_junit_rejects_any_skip(tmp_path: Path) -> None:
    report = _write_junit(tmp_path, list(REQUIRED_CASES), skipped=REQUIRED_CASES[0])

    with pytest.raises(NativeReleaseError, match="skipped cases"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_failures(tmp_path: Path) -> None:
    report = _write_junit(
        tmp_path, [*REQUIRED_CASES, "test_extra_case"], failure="test_extra_case"
    )

    with pytest.raises(NativeReleaseError, match="failures"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_missing_named_case(tmp_path: Path) -> None:
    report = _write_junit(tmp_path, list(REQUIRED_CASES[1:]))

    with pytest.raises(NativeReleaseError, match="required release cases absent"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_an_empty_report(tmp_path: Path) -> None:
    report = _write_junit(tmp_path, [])

    with pytest.raises(NativeReleaseError, match="no test cases"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_a_nonzero_pytest_rc(tmp_path: Path) -> None:
    report = _write_junit(tmp_path, list(REQUIRED_CASES))

    with pytest.raises(NativeReleaseError, match="pytest exited 1"):
        validate_junit(report, pytest_rc=1)


def test_junit_rejects_declared_counts_that_hide_skips(tmp_path: Path) -> None:
    report = _write_junit(tmp_path, list(REQUIRED_CASES), declared_skipped=2)

    with pytest.raises(NativeReleaseError, match="skipped cases"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_an_incomplete_report(tmp_path: Path) -> None:
    report = _write_junit(
        tmp_path, list(REQUIRED_CASES), declared_tests=len(REQUIRED_CASES) + 2
    )

    with pytest.raises(NativeReleaseError, match="incomplete JUnit report"):
        validate_junit(report, pytest_rc=0)


def test_junit_rejects_a_missing_report(tmp_path: Path) -> None:
    with pytest.raises(NativeReleaseError, match="was not written"):
        validate_junit(tmp_path / "absent.xml", pytest_rc=0)


def test_pinned_refs_are_read_without_importing_app_code(tmp_path: Path) -> None:
    app = tmp_path / "app"
    gate = app / "tests" / "release" / "fleet_gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text(
        "# module must never execute during validation\n"
        'raise SystemExit("the workflow gate imported app code")\n'
        f'PREVIOUS = "{PREVIOUS}"\n'
        f'REGISTRY = "{REGISTRY}"\n',
        encoding="utf-8",
    )

    assert pinned_release_refs(app) == {  # nosec B101
        "previous": PREVIOUS,
        "registry": REGISTRY,
    }


@pytest.mark.parametrize(
    ("previous", "message"),
    [
        ("dub19/gbrain-aio:latest", "not name@sha256"),
        ("dub19/gbrain-aio@sha256:" + "A" * 64, "not name@sha256"),
        ("other/image@sha256:" + "a" * 64, "reviewed repository"),
    ],
)
def test_pinned_refs_reject_mutable_or_foreign_inputs(
    tmp_path: Path, previous: str, message: str
) -> None:
    app = tmp_path / "app"
    gate = app / "tests" / "release" / "fleet_gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text(f'PREVIOUS = "{previous}"\nREGISTRY = "{REGISTRY}"\n')

    with pytest.raises(NativeReleaseError, match=message):
        pinned_release_refs(app)


def test_pinned_refs_require_the_merged_suite(tmp_path: Path) -> None:
    with pytest.raises(NativeReleaseError, match="merged release suite"):
        pinned_release_refs(tmp_path / "missing")


def _image(ref: str, image_id: str, *, arch: str = "arm64") -> dict:
    return {
        "ref": ref,
        "id": image_id,
        "arch": arch,
        "digest_relationship": True,
    }


def _receipt(
    *,
    verdict: str = "PASS",
    reservation: dict | None = None,
    run_id: str = "aio-rel-aaaaaaaa",
    arch: str = "arm64",
    previous_id: str = PREVIOUS_ID,
    candidate_id: str = CANDIDATE_ID,
    prefix: str | None = None,
    network_mode: str = "none",
) -> dict:
    prefix = prefix or run_id
    return {
        "suite": "tests/release (gbrain-aio release qualification)",
        "run_id": run_id,
        "verdict": verdict,
        "classification": "distinct-upgrade",
        "platform_arch": arch,
        "resource_prefix": prefix,
        "inputs": {
            "arch": arch,
            "hosted_github_mode": True,
            "socket_mode": "host-default",
        },
        "dind": {"server_arch": arch, "socket_mode": "host-default"},
        "checkout": {"git_head": SHA},
        "images": {
            "previous": _image(PREVIOUS, previous_id, arch=arch),
            "candidate": _image(CANDIDATE, candidate_id, arch=arch),
        },
        "volumes": {
            "data": f"{prefix}-data",
            "brain": f"{prefix}-brain",
        },
        "containers": {
            "previous": f"{prefix}-previous",
            "candidate": f"{prefix}-candidate",
        },
        "reservation": dict(
            reservation if reservation is not None else SAVED_RESERVATION
        ),
        "stages": {
            stage: {
                "https": "ok",
                "network_mode": network_mode,
                "cluster_id": "7421",
            }
            for stage in ("before", "after", "restart")
        },
    }


def _both_arms() -> list[dict]:
    return [
        _receipt(),
        _receipt(
            verdict="CONTROL: WRITE BLOCKED",
            reservation=MISSING_RESERVATION,
            run_id="aio-rel-bbbbbbbb",
        ),
    ]


def _check(receipts: list[dict], **overrides: object) -> dict:
    expected: dict = {
        "expected_sha": SHA,
        "expected_previous_image": PREVIOUS,
        "expected_previous_id": PREVIOUS_ID,
        "expected_candidate_image": CANDIDATE,
        "expected_candidate_id": CANDIDATE_ID,
    }
    expected.update(overrides)
    return validate_receipts(receipts, **expected)


def test_receipts_require_both_ownership_arms() -> None:
    summary = _check(_both_arms(), expected_sha=SHA)

    assert summary["arch"] == "arm64"  # nosec B101
    assert summary["classification"] == "distinct-upgrade"  # nosec B101
    assert summary["previous_id"] != summary["candidate_id"]  # nosec B101
    assert len(summary["run_ids"]) == 2  # nosec B101
    assert summary["verdicts"] == [  # nosec B101
        "PASS",
        "CONTROL: WRITE BLOCKED",
    ]
    assert summary["reservation"]["missing"] == MISSING_RESERVATION  # nosec B101


def test_receipts_reject_one_arm_or_duplicate_run_id() -> None:
    with pytest.raises(NativeReleaseError, match="exactly two ownership receipts"):
        _check([_receipt()])

    duplicate = _both_arms()
    duplicate[1]["run_id"] = duplicate[0]["run_id"]
    with pytest.raises(NativeReleaseError, match="run ids are not unique"):
        _check(duplicate)


def test_receipts_reject_the_same_image_for_both_roles() -> None:
    receipts = _both_arms()
    receipts[0]["images"]["candidate"]["id"] = PREVIOUS_ID

    with pytest.raises(NativeReleaseError, match="same image id"):
        _check(receipts)


def test_receipts_reject_non_native_or_unpinned_evidence() -> None:
    receipts = _both_arms()
    receipts[0]["platform_arch"] = "amd64"
    with pytest.raises(NativeReleaseError, match="platform arch is not arm64"):
        _check(receipts)

    receipts = _both_arms()
    receipts[0]["dind"]["server_arch"] = "amd64"
    with pytest.raises(NativeReleaseError, match="daemon arch is not arm64"):
        _check(receipts)

    receipts = _both_arms()
    receipts[0]["images"]["candidate"]["digest_relationship"] = False
    with pytest.raises(NativeReleaseError, match="does not describe its image"):
        _check(receipts)


def test_receipts_reject_an_unqualified_outcome_or_wrong_reservation() -> None:
    receipts = _both_arms()
    receipts[0]["verdict"] = "FAIL"
    with pytest.raises(NativeReleaseError, match="recorded FAIL"):
        _check(receipts)

    receipts = _both_arms()
    receipts[1]["reservation"] = SAVED_RESERVATION
    with pytest.raises(NativeReleaseError, match="saved-reservation arm"):
        _check(receipts)

    receipts = _both_arms()
    receipts[0]["verdict"] = "CONTROL: WRITE BLOCKED"
    with pytest.raises(NativeReleaseError, match="did not PASS"):
        _check(receipts)


def test_receipts_reject_a_different_checkout_or_unsafe_resources() -> None:
    receipts = _both_arms()
    receipts[0]["checkout"]["git_head"] = "f" * 40
    with pytest.raises(NativeReleaseError, match="is not the dispatched"):
        _check(receipts, expected_sha=SHA)

    receipts = _both_arms()
    receipts[0]["resource_prefix"] = "prod"
    with pytest.raises(NativeReleaseError, match="resource prefix"):
        _check(receipts)

    receipts = _both_arms()
    receipts[0]["stages"]["after"]["network_mode"] = "host"
    with pytest.raises(NativeReleaseError, match="not network-isolated"):
        _check(receipts)


def test_receipts_are_bound_to_the_workflows_exact_images() -> None:
    other_candidate = "127.0.0.1:39999/gbrain-arm64-999-1@sha256:" + "d" * 64
    receipts = _both_arms()
    for receipt in receipts:
        receipt["images"]["candidate"]["ref"] = other_candidate
    with pytest.raises(NativeReleaseError, match="workflow staged"):
        _check(receipts)

    receipts = _both_arms()
    for receipt in receipts:
        receipt["images"]["previous"]["id"] = "sha256:" + "9" * 64
    with pytest.raises(NativeReleaseError, match="workflow pulled"):
        _check(receipts)

    receipts = _both_arms()
    receipts[1]["images"]["candidate"]["id"] = "sha256:" + "8" * 64
    with pytest.raises(NativeReleaseError, match="same pinned images"):
        _check(receipts)


def test_receipts_reject_a_non_loopback_expected_candidate() -> None:
    with pytest.raises(NativeReleaseError, match="not a loopback-staged ref"):
        _check(
            _both_arms(),
            expected_candidate_image="dub19/gbrain-aio@sha256:" + "a" * 64,
        )


def test_summary_records_run_ids_and_disabled_publishing() -> None:
    text = summary_markdown(_check(_both_arms(), expected_sha=SHA))

    assert "native ARM64 release qualification" in text  # nosec B101
    assert "Publishing: disabled" in text  # nosec B101
    assert "aio-rel-aaaaaaaa" in text  # nosec B101
    assert "distinct-upgrade" in text  # nosec B101


def test_cli_reads_receipt_files_and_writes_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "receipts"
    directory.mkdir()
    for receipt in _both_arms():
        (directory / f"{receipt['run_id']}.json").write_text(json.dumps(receipt))
    summary = tmp_path / "summary.md"

    rc = main(
        [
            "receipts",
            str(directory),
            "--expected-sha",
            SHA,
            "--expected-previous",
            PREVIOUS,
            "--expected-previous-id",
            PREVIOUS_ID,
            "--expected-candidate",
            CANDIDATE,
            "--expected-candidate-id",
            CANDIDATE_ID,
            "--summary",
            str(summary),
        ]
    )

    assert rc == 0  # nosec B101
    assert len(load_receipts(directory)) == 2  # nosec B101
    assert "Publishing: disabled" in summary.read_text()  # nosec B101
    assert "aio-rel-bbbbbbbb" in capsys.readouterr().out  # nosec B101


def test_cli_fails_closed_on_bad_receipts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "receipts"
    directory.mkdir()
    (directory / "only.json").write_text(json.dumps(_receipt()))

    assert (  # nosec B101
        main(
            [
                "receipts",
                str(directory),
                "--expected-sha",
                SHA,
                "--expected-previous",
                PREVIOUS,
                "--expected-previous-id",
                PREVIOUS_ID,
                "--expected-candidate",
                CANDIDATE,
                "--expected-candidate-id",
                CANDIDATE_ID,
            ]
        )
        == 1
    )
    assert "native-release gate failed" in capsys.readouterr().err  # nosec B101


def test_module_exposes_the_required_case_set() -> None:
    assert len(REQUIRED_CASES) == len(set(REQUIRED_CASES))  # nosec B101
    assert (  # nosec B101
        "test_missing_reservation_fails_closed_not_a_qualified_upgrade"
        in REQUIRED_CASES
    )
    assert (  # nosec B101
        "test_saved_reservation_upgrade_writes_after_replace_and_restart"
        in REQUIRED_CASES
    )
    assert (
        "test_candidate_takes_a_new_write_after_restart" in REQUIRED_CASES
    )  # nosec B101
    assert native_release.PREVIOUS_REPOSITORY == "dub19/gbrain-aio"  # nosec B101
