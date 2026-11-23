"""Pure validation for the manual native-ARM64 GBrain release qualification.

The workflow in ``.github/workflows/gbrain-arm64-release-qualification.yml``
is a read-only, no-publish test dispatch. It runs the app's exact merged
``tests/release`` suite on a native arm64 hosted runner and then calls this
module to fail closed on the run's own evidence:

* ``refs`` reads the digest pins from the checked-out app's
  ``tests/release/fleet_gate.py`` without importing or executing it.
* ``junit`` requires the named release cases and zero skips/failures/errors.
* ``receipts`` requires both ownership arms, native arm64, distinct pinned
  images, disposable resource names, and the exact checkout SHA under test.

Everything here is stdlib-only and never touches Docker or the network.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from defusedxml import ElementTree as ET

from aio_fleet.strict_pytest import JUnitReportError, parse_junit_summary

PREVIOUS_REPOSITORY = "dub19/gbrain-aio"
REGISTRY_REPOSITORY = "registry"
PINNED_REF = re.compile(r"^(?P<name>[^\s@]+)@sha256:[0-9a-f]{64}$")
SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

REQUIRED_CASES = (
    "test_daemon_identity_and_classification_are_declared",
    "test_digest_pinned_inputs_describe_the_images_under_test",
    "test_images_resolve_to_two_distinct_ids",
    "test_missing_reservation_fails_closed_not_a_qualified_upgrade",
    "test_only_uniquely_named_volumes_were_touched",
    "test_paid_model_surface_is_disabled",
    "test_persisted_record_readback_old_to_candidate",
    "test_retrieval_parity_before_vs_after_upgrade",
    "test_saved_reservation_upgrade_writes_after_replace_and_restart",
    "test_candidate_takes_a_new_write_after_restart",
)

SAVED_RESERVATION = {
    "baseline": "matching-backup",
    "after": "write-verified",
    "restart": "write-verified",
}
MISSING_RESERVATION = {
    "baseline": "no-backup",
    "after": "recovery_required",
    "restart": "recovery_required",
}


class NativeReleaseError(ValueError):
    """The dispatch structure or its evidence does not meet the contract."""


# --- Pinned inputs ----------------------------------------------------------
def _parse_pinned_constant(name: str, value: object, repository: str) -> str:
    if not isinstance(value, str):
        raise NativeReleaseError(f"{name} is not a string")
    match = PINNED_REF.fullmatch(value)
    if match is None:
        raise NativeReleaseError(
            f"{name}={value!r} is not name@sha256:<64 lowercase hex>"
        )
    if match.group("name") != repository:
        raise NativeReleaseError(
            f"{name}={value!r} does not name the reviewed repository {repository!r}"
        )
    return value


def pinned_release_refs(app_dir: Path | str) -> dict[str, str]:
    """Read PREVIOUS/REGISTRY digest pins from the app checkout, without import."""
    path = Path(app_dir) / "tests" / "release" / "fleet_gate.py"
    if not path.is_file():
        raise NativeReleaseError(
            f"{path} is missing; the checkout does not carry the merged release suite"
        )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"PREVIOUS", "REGISTRY"}:
                found[target.id] = ast.literal_eval(node.value)
    missing = {"PREVIOUS", "REGISTRY"} - set(found)
    if missing:
        raise NativeReleaseError(f"{path} has no module-level {sorted(missing)}")
    return {
        "previous": _parse_pinned_constant(
            "PREVIOUS", found["PREVIOUS"], PREVIOUS_REPOSITORY
        ),
        "registry": _parse_pinned_constant(
            "REGISTRY", found["REGISTRY"], REGISTRY_REPOSITORY
        ),
    }


# --- JUnit ------------------------------------------------------------------
def validate_junit(path: Path | str, *, pytest_rc: int | None = None) -> dict:
    """Require every named release case, with zero skips, failures or errors.

    Counts come from the fleet's strict-pytest parser, so declared attributes
    cannot under-count the rows they describe and an incomplete report fails
    closed instead of looking green.
    """
    junit = Path(path)
    if not junit.is_file():
        raise NativeReleaseError(f"JUnit report {junit} was not written")
    if pytest_rc is None:
        raise NativeReleaseError("pytest exit code is required for the JUnit gate")
    if pytest_rc != 0:
        raise NativeReleaseError(f"pytest exited {pytest_rc}")
    try:
        summary = parse_junit_summary(junit)
    except JUnitReportError as exc:
        raise NativeReleaseError(
            f"untrusted JUnit report ({exc.reason}): {exc}"
        ) from exc
    if summary.tests == 0:
        raise NativeReleaseError(f"{junit} contains no test cases")
    root = ET.parse(junit).getroot()
    if root is None:  # pragma: no cover - defensive for optional stub typing
        raise NativeReleaseError(f"{junit} has no root element")
    cases = root.findall(".//testcase")
    names = [case.get("name") or "" for case in cases]
    if any(not name for name in names):
        raise NativeReleaseError(f"{junit} has an unnamed test case")
    skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
    failures = [case.get("name") for case in cases if case.find("failure") is not None]
    errors = [case.get("name") for case in cases if case.find("error") is not None]
    if summary.skipped:
        raise NativeReleaseError(f"release suite skipped cases: {skipped}")
    if summary.failures:
        raise NativeReleaseError(f"release suite failures: {failures}")
    if summary.errors:
        raise NativeReleaseError(f"release suite errors: {errors}")
    if summary.cases != summary.tests:
        raise NativeReleaseError(
            f"incomplete JUnit report: declared {summary.tests} test cases, "
            f"found {summary.cases} testcase rows"
        )
    present = set(names)
    if len(present) != len(names):
        raise NativeReleaseError(f"{junit} repeats a test case name")
    missing = sorted(set(REQUIRED_CASES) - present)
    if missing:
        raise NativeReleaseError(f"required release cases absent: {missing}")
    return {
        "tests": summary.tests,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "required_cases_present": True,
        "case_names": sorted(present),
    }


# --- Receipts ---------------------------------------------------------------
def _require(condition: object, message: str) -> None:
    if not condition:
        raise NativeReleaseError(message)


def _require_pinned(ref: object, role: str) -> str:
    if not isinstance(ref, str):
        raise NativeReleaseError(f"{role} ref is not a string")
    if PINNED_REF.fullmatch(ref) is None:
        raise NativeReleaseError(f"{role}={ref!r} is not digest-pinned")
    return ref


def _validate_images(receipt: Mapping, arm: str) -> tuple[str, str, str, str]:
    inputs = receipt.get("inputs") or {}
    _require(inputs.get("arch") == arm, f"{arm}: input arch is not {arm}")
    _require(
        inputs.get("hosted_github_mode") is True, f"{arm}: not verified hosted mode"
    )
    _require(
        inputs.get("socket_mode") == "host-default", f"{arm}: unexpected socket mode"
    )

    images = receipt.get("images") or {}
    previous, candidate = images.get("previous") or {}, images.get("candidate") or {}
    previous_ref = _require_pinned(previous.get("ref"), f"{arm} previous")
    candidate_ref = _require_pinned(candidate.get("ref"), f"{arm} candidate")
    _require(
        previous_ref.startswith(PREVIOUS_REPOSITORY + "@"),
        f"{arm}: previous ref is not the reviewed baseline",
    )
    for role, image in (("previous", previous), ("candidate", candidate)):
        image_id = str(image.get("id") or "")
        _require(
            image.get("arch") == arm,
            f"{arm}: {role} image architecture is {image.get('arch')!r}",
        )
        _require(
            SHA256_ID.fullmatch(image_id) is not None,
            f"{arm}: {role} image id is not sha256",
        )
        _require(
            image.get("digest_relationship") is True,
            f"{arm}: pinned {role} ref does not describe its image",
        )
    previous_id = str(previous.get("id") or "")
    candidate_id = str(candidate.get("id") or "")
    _require(
        previous_id != candidate_id,
        f"{arm}: previous and candidate are the same image id",
    )
    return previous_ref, previous_id, candidate_ref, candidate_id


def _validate_stages(receipt: Mapping, arm: str) -> None:
    stages = receipt.get("stages") or {}
    for name in ("before", "after", "restart"):
        stage = stages.get(name)
        if not isinstance(stage, dict):
            raise NativeReleaseError(f"{arm}: stage {name} is missing")
        _require(stage.get("https") == "ok", f"{arm}: stage {name} is not healthy")
        _require(
            stage.get("network_mode") == "none",
            f"{arm}: stage {name} is not network-isolated",
        )
        _require(stage.get("cluster_id"), f"{arm}: stage {name} lost cluster identity")


def _validate_resources(receipt: Mapping, arm: str) -> None:
    prefix = receipt.get("resource_prefix") or ""
    _require(prefix.startswith("aio-rel-"), f"{arm}: resource prefix is not disposable")
    volumes = receipt.get("volumes") or {}
    _require(
        sorted(volumes.values()) == [f"{prefix}-brain", f"{prefix}-data"],
        f"{arm}: volumes are not the run's two disposable volumes",
    )
    containers = receipt.get("containers") or {}
    for role in ("previous", "candidate"):
        _require(
            str(containers.get(role) or "").startswith(prefix + "-"),
            f"{arm}: {role} container is not run-scoped",
        )


def validate_receipts(
    receipts: Iterable[Mapping],
    *,
    expected_sha: str,
    expected_previous_image: str,
    expected_previous_id: str,
    expected_candidate_image: str,
    expected_candidate_id: str,
    arm: str = "arm64",
) -> dict:
    """Validate both ownership arms and bind them to the workflow's own inputs.

    The expected refs/ids are the exact values the workflow pulled and staged;
    a receipt that names any other image is not evidence for this run.
    """
    if not expected_candidate_image.startswith("127.0.0.1:"):
        raise NativeReleaseError(
            "expected candidate is not a loopback-staged ref: "
            f"{expected_candidate_image!r}"
        )
    _require_pinned(expected_previous_image, "expected previous")
    _require_pinned(expected_candidate_image, "expected candidate")
    if SHA256_ID.fullmatch(expected_previous_id) is None:
        raise NativeReleaseError(
            f"expected previous id is not sha256: {expected_previous_id!r}"
        )
    if SHA256_ID.fullmatch(expected_candidate_id) is None:
        raise NativeReleaseError(
            f"expected candidate id is not sha256: {expected_candidate_id!r}"
        )
    if COMMIT_SHA.fullmatch(expected_sha) is None:
        raise NativeReleaseError(
            f"expected checkout is not a commit SHA: {expected_sha!r}"
        )
    records = [dict(receipt) for receipt in receipts]
    _require(
        len(records) == 2,
        f"expected exactly two ownership receipts, found {len(records)}",
    )
    for receipt in records:
        _require(receipt.get("verdict") != "FAIL", "a receipt recorded FAIL")
        _require(
            receipt.get("classification") == "distinct-upgrade",
            "receipt is not a distinct upgrade",
        )
        _require(receipt.get("platform_arch") == arm, f"platform arch is not {arm}")
        dind = receipt.get("dind") or {}
        _require(dind.get("server_arch") == arm, f"daemon arch is not {arm}")
        _require(
            dind.get("socket_mode") == "host-default", "daemon is not the hosted socket"
        )
        checkout = receipt.get("checkout") or {}
        _require(
            checkout.get("git_head") == expected_sha,
            f"checkout {checkout.get('git_head')!r} is not the dispatched {expected_sha}",
        )

    saved = [r for r in records if r.get("reservation") == SAVED_RESERVATION]
    missing = [r for r in records if r.get("reservation") == MISSING_RESERVATION]
    _require(len(saved) == 1, "saved-reservation arm is missing or duplicated")
    _require(len(missing) == 1, "missing-reservation control is missing or duplicated")

    pass_arm, control_arm = saved[0], missing[0]
    _require(pass_arm.get("verdict") == "PASS", "saved-reservation arm did not PASS")
    _require(
        control_arm.get("verdict") == "CONTROL: WRITE BLOCKED",
        "missing-reservation arm did not record the blocked-write control",
    )

    identities = []
    for name, receipt in (("PASS", pass_arm), ("CONTROL", control_arm)):
        _validate_stages(receipt, f"{name} arm")
        _validate_resources(receipt, f"{name} arm")
        identities.append(_validate_images(receipt, arm))

    previous_ref, previous_id, candidate_ref, candidate_id = identities[0]
    _require(
        identities[0] == identities[1],
        "the two arms did not test the same pinned images",
    )
    _require(
        previous_ref == expected_previous_image,
        f"receipts tested previous {previous_ref!r}, workflow pulled "
        f"{expected_previous_image!r}",
    )
    _require(
        previous_id == expected_previous_id,
        f"receipts tested previous id {previous_id!r}, workflow pulled "
        f"{expected_previous_id!r}",
    )
    _require(
        candidate_ref == expected_candidate_image,
        f"receipts tested candidate {candidate_ref!r}, workflow staged "
        f"{expected_candidate_image!r}",
    )
    _require(
        candidate_id == expected_candidate_id,
        f"receipts tested candidate id {candidate_id!r}, workflow staged "
        f"{expected_candidate_id!r}",
    )
    run_ids = [str(r.get("run_id") or "") for r in records]
    _require(
        all(run_id.startswith("aio-rel-") for run_id in run_ids),
        f"receipt run ids are not verifiable: {run_ids}",
    )
    _require(len(set(run_ids)) == len(run_ids), "receipt run ids are not unique")
    return {
        "verdicts": [pass_arm["verdict"], control_arm["verdict"]],
        "run_ids": run_ids,
        "arch": arm,
        "previous_image": previous_ref,
        "previous_id": previous_id,
        "candidate_image": candidate_ref,
        "candidate_id": candidate_id,
        "classification": "distinct-upgrade",
        "reservation": {
            "saved": dict(SAVED_RESERVATION),
            "missing": dict(MISSING_RESERVATION),
        },
    }


def load_receipts(directory: Path | str) -> list[dict]:
    path = Path(directory)
    if not path.is_dir():
        raise NativeReleaseError(f"receipt directory {path} is missing")
    receipts: list[dict] = []
    for candidate in sorted(path.glob("*.json")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NativeReleaseError(f"{candidate} is not JSON: {exc}") from exc
        if isinstance(value, dict):
            receipts.append(value)
    if not receipts:
        raise NativeReleaseError(f"{path} contains no receipt JSON")
    return receipts


def summary_markdown(summary: Mapping) -> str:
    lines = [
        "## GBrain native ARM64 release qualification",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Arch | `{summary['arch']}` (native hosted runner) |",
        f"| Previous | `{summary['previous_image']}` (`{summary['previous_id']}`) |",
        f"| Candidate | `{summary['candidate_image']}` (`{summary['candidate_id']}`) |",
        f"| Classification | `{summary['classification']}` |",
        f"| Verdicts | {', '.join(f'`{v}`' for v in summary['verdicts'])} |",
        f"| Run IDs | {', '.join(f'`{r}`' for r in summary['run_ids'])} |",
        "",
        "Publishing: disabled. No registry login, public push, tag or artifact.",
        "",
    ]
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------
def _write_github_output(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    refs = sub.add_parser("refs", help="read digest pins from the app checkout")
    refs.add_argument("app_dir", type=Path)
    refs.add_argument("--github-output", type=Path)

    junit = sub.add_parser("junit", help="require named cases with zero skips")
    junit.add_argument("report", type=Path)
    junit.add_argument("--pytest-rc", type=int, required=True)

    receipts = sub.add_parser("receipts", help="validate both ownership arms")
    receipts.add_argument("directory", type=Path)
    receipts.add_argument("--expected-sha", required=True)
    receipts.add_argument("--expected-previous", required=True)
    receipts.add_argument("--expected-previous-id", required=True)
    receipts.add_argument("--expected-candidate", required=True)
    receipts.add_argument("--expected-candidate-id", required=True)
    receipts.add_argument("--summary", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "refs":
            result = pinned_release_refs(args.app_dir)
            if args.github_output:
                _write_github_output(args.github_output, result)
        elif args.command == "junit":
            result = validate_junit(args.report, pytest_rc=args.pytest_rc)
        else:
            result = validate_receipts(
                load_receipts(args.directory),
                expected_sha=args.expected_sha,
                expected_previous_image=args.expected_previous,
                expected_previous_id=args.expected_previous_id,
                expected_candidate_image=args.expected_candidate,
                expected_candidate_id=args.expected_candidate_id,
            )
            if args.summary:
                args.summary.write_text(summary_markdown(result), encoding="utf-8")
    except NativeReleaseError as exc:
        print(f"native-release gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
