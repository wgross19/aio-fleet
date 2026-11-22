"""Pure validation for the manual native-ARM64 honcho-aio release qualification.

The workflow in ``.github/workflows/honcho-arm64-release-qualification.yml``
is a read-only, no-publish test dispatch in the verified GBrain runner-local
model: build once from the trusted app-main checkout, stage that exact image
once on a disposable loopback registry, test the staged digest with the
manager-reviewed test-ref suite, and fail closed on the run's own evidence.
This module validates that evidence without touching Docker or the network:

* ``junit`` requires honcho's named actual-Docker cases, zero skips, zero
  failures/errors, and complete counts — measured by the central strict
  parser (``aio_fleet.strict_pytest.parse_junit_summary``), so a report whose
  declared attributes under-count its rows fails instead of looking green.
* ``receipts`` requires exactly the two honcho scenarios — the same-image
  control and the distinct two-version upgrade — bound to the exact
  previous digest the workflow pinned and the exact loopback candidate it
  staged, with mode/arch, the disk write-path count chain, and unique
  run-scoped resources all verified.

Everything here is stdlib-plus-defusedxml only. Expected values always arrive
as CLI arguments; nothing is interpolated into this module by a shell.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

# defusedxml everywhere: the JUnit report is pytest-generated on the same
# runtime, but the gate never relies on stdlib XML entity handling.
from defusedxml import ElementTree as ET

from aio_fleet.strict_pytest import JUnitReportError, parse_junit_summary

PREVIOUS_REPOSITORY = "dub19/honcho-aio"
PINNED_REF = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_TOKEN = re.compile(r"^[0-9a-f]{6,16}$")

# The named cases that actually drive Docker. The wrapper/negative cases run
# in the same suite; these three prove the engine gate and both choreographies.
REQUIRED_JUNIT_CASES = (
    "test_docker_engine_verified",
    "test_same_digest_smoke",
    "test_two_version_upgrade",
)

# Scenario -> the classification the suite must self-report. The same-image
# control proves the harness; only the distinct-digest arm upgrades.
SCENARIO_CLASSIFICATIONS = {
    "same_digest_smoke": "harness-sanity-same-digest",
    "two_version_upgrade": "two-version-upgrade",
}

COUNT_CHAIN = (
    # (key, must equal previous key + delta); the chain starts at 0.
    ("disk_before_seed", None, 0),
    ("disk_after_seed", "disk_before_seed", 1),
    ("disk_after_replace", "disk_after_seed", 0),
    ("disk_after_write_on_candidate", "disk_after_replace", 1),
    ("disk_after_restart", "disk_after_write_on_candidate", 0),
    ("disk_after_post_restart_write", "disk_after_restart", 1),
)


class HonchoGateError(ValueError):
    """The dispatch structure or its evidence does not meet the contract."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise HonchoGateError(message)


def _require_pinned(ref: object, role: str) -> str:
    if not isinstance(ref, str):
        raise HonchoGateError(f"{role} ref is not a string")
    if PINNED_REF.fullmatch(ref) is None:
        raise HonchoGateError(f"{role}={ref!r} is not name@sha256:<64 hex>")
    return ref


# --- JUnit ------------------------------------------------------------------
def validate_junit(path: Path | str, *, pytest_rc: int) -> dict:
    """Require honcho's named Docker cases with zero skips and complete counts."""
    junit = Path(path)
    if not junit.is_file():
        raise HonchoGateError(f"JUnit report {junit} was not written")
    if pytest_rc != 0:
        raise HonchoGateError(f"pytest exited {pytest_rc}")
    try:
        summary = parse_junit_summary(junit)
    except JUnitReportError as exc:
        raise HonchoGateError(f"untrusted JUnit report ({exc.reason}): {exc}") from exc
    if summary.tests == 0:
        raise HonchoGateError(f"{junit} contains no test cases")
    root = ET.parse(junit).getroot()
    if root is None:  # pragma: no cover - defensive for optional stub typing
        raise HonchoGateError(f"{junit} has no root element")
    cases = root.findall(".//testcase")
    names = [case.get("name") or "" for case in cases]
    if any(not name for name in names):
        raise HonchoGateError(f"{junit} has an unnamed test case")
    skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
    failures = [case.get("name") for case in cases if case.find("failure") is not None]
    errors = [case.get("name") for case in cases if case.find("error") is not None]
    if summary.skipped:
        raise HonchoGateError(f"release suite skipped cases: {skipped}")
    if summary.failures:
        raise HonchoGateError(f"release suite failures: {failures}")
    if summary.errors:
        raise HonchoGateError(f"release suite errors: {errors}")
    if summary.cases != summary.tests:
        raise HonchoGateError(
            f"incomplete JUnit report: declared {summary.tests} test cases, "
            f"found {summary.cases} testcase rows"
        )
    if len(set(names)) != len(names):
        raise HonchoGateError(f"{junit} repeats a test case name")
    missing = sorted(set(REQUIRED_JUNIT_CASES) - set(names))
    if missing:
        raise HonchoGateError(f"required Docker cases absent: {missing}")
    return {
        "tests": summary.tests,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "required_cases_present": True,
        "case_names": sorted(set(names)),
    }


# --- Receipts ---------------------------------------------------------------
def _validate_count_chain(receipt: Mapping, label: str) -> int:
    counts = receipt.get("disk_message_counts")
    if not isinstance(counts, dict):
        raise HonchoGateError(f"{label}: disk_message_counts is missing")
    values = dict(counts)
    previous_key = None
    previous_value = 0
    for key, base_key, delta in COUNT_CHAIN:
        raw = values.pop(key, None)
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise HonchoGateError(f"{label}: {key} is missing or not an int")
        expected = (previous_value + delta) if base_key is not None else delta
        if raw != expected:
            raise HonchoGateError(
                f"{label}: disk write-path chain broke at {key}: "
                f"{raw} != {expected}"
            )
        previous_key, previous_value = key, raw
    if values:
        raise HonchoGateError(
            f"{label}: unexpected extra disk count keys: {sorted(values)}"
        )
    assert previous_key == "disk_after_post_restart_write"
    return previous_value


def _validate_unique_owned_resources(receipt: Mapping, label: str) -> None:
    token = receipt.get("run_token")
    if not isinstance(token, str) or RUN_TOKEN.fullmatch(token) is None:
        raise HonchoGateError(f"{label}: run_token {token!r} is not run-scoped")
    workspace = receipt.get("workspace")
    if not isinstance(workspace, str) or not workspace.startswith("honcho-rq-"):
        raise HonchoGateError(
            f"{label}: workspace {workspace!r} does not use the disposable "
            "honcho-rq- prefix"
        )
    if not workspace.endswith("-ws"):
        raise HonchoGateError(f"{label}: workspace {workspace!r} is not a -ws name")
    scenario = receipt.get("scenario")
    contents = receipt.get("contents")
    if not isinstance(contents, dict):
        raise HonchoGateError(f"{label}: contents are missing")
    # The suite's receipt keys use past-tense stage names while the message
    # itself carries the choreography stage word (verified on the actual
    # AMD64 run receipts: key after_replace -> '...-replace-<token>').
    expected = {
        "seed": f"release-quality-{scenario}-seed-{token}",
        "after_replace": f"release-quality-{scenario}-replace-{token}",
        "after_restart": f"release-quality-{scenario}-restart-{token}",
    }
    if contents != expected:
        raise HonchoGateError(
            f"{label}: contents are not the run's deterministic three-stage "
            f"messages: {contents!r}"
        )
    message_ids = {
        receipt.get("message_id"),
        receipt.get("write_after_replace_id"),
        receipt.get("write_after_restart_id"),
    }
    if None in message_ids or len(message_ids) != 3:
        raise HonchoGateError(
            f"{label}: the seeded message and the two new writes do not carry "
            f"three distinct ids: {sorted(str(i) for i in message_ids)}"
        )


def validate_receipts(
    receipts: Iterable[Mapping],
    *,
    expected_mode: str,
    expected_arch: str,
    expected_previous_image: str,
    expected_previous_id: str,
    expected_candidate_image: str,
    expected_candidate_id: str,
) -> dict:
    """Validate both honcho scenarios and bind them to the workflow's inputs.

    The expected refs/ids are the exact values the workflow pinned and
    staged; a receipt naming any other image is not evidence for this run.
    ``expected_mode``/``expected_arch`` make the verifier reusable on the
    verified local DinD path (``dind-tls``/``amd64``) as well as the hosted
    ARM64 path — an AMD64-local receipt presented as ARM64 hosted evidence is
    rejected, and vice versa.
    """
    _require_pinned(expected_previous_image, "expected previous")
    _require(
        expected_previous_image.startswith(PREVIOUS_REPOSITORY + "@"),
        f"expected previous does not name the reviewed baseline repository "
        f"{PREVIOUS_REPOSITORY!r}: {expected_previous_image!r}",
    )
    _require_pinned(expected_candidate_image, "expected candidate")
    _require(
        expected_candidate_image.startswith("127.0.0.1:"),
        f"expected candidate is not a loopback-staged ref: "
        f"{expected_candidate_image!r}",
    )
    if SHA256_ID.fullmatch(expected_previous_id) is None:
        raise HonchoGateError(
            f"expected previous id is not sha256: {expected_previous_id!r}"
        )
    if SHA256_ID.fullmatch(expected_candidate_id) is None:
        raise HonchoGateError(
            f"expected candidate id is not sha256: {expected_candidate_id!r}"
        )
    if expected_arch not in ("amd64", "arm64"):
        raise HonchoGateError(f"expected arch is not amd64|arm64: {expected_arch!r}")

    records = [dict(receipt) for receipt in receipts]
    if len(records) != 2:
        raise HonchoGateError(
            f"expected exactly two scenario receipts, found {len(records)}"
        )
    by_scenario: dict[str, dict] = {}
    for receipt in records:
        scenario = receipt.get("scenario")
        if scenario not in SCENARIO_CLASSIFICATIONS:
            raise HonchoGateError(f"receipt carries an unknown scenario: {scenario!r}")
        if scenario in by_scenario:
            raise HonchoGateError(f"scenario {scenario!r} appears twice")
        by_scenario[scenario] = receipt
    missing = set(SCENARIO_CLASSIFICATIONS) - set(by_scenario)
    if missing:
        raise HonchoGateError(f"scenario receipts absent: {sorted(missing)}")

    expected_arch_field = f"linux/{expected_arch}/linux/{expected_arch}"
    for scenario, receipt in sorted(by_scenario.items()):
        label = f"{scenario}"
        _require(
            receipt.get("classification") == SCENARIO_CLASSIFICATIONS[scenario],
            f"{label}: classification is not "
            f"{SCENARIO_CLASSIFICATIONS[scenario]!r}",
        )
        _require(
            receipt.get("docker_endpoint_mode") == expected_mode,
            f"{label}: docker_endpoint_mode {receipt.get('docker_endpoint_mode')!r} "
            f"is not the expected mode {expected_mode!r}",
        )
        _require(
            receipt.get("arch") == expected_arch_field,
            f"{label}: arch {receipt.get('arch')!r} is not {expected_arch_field!r} "
            "(mode/arch mismatch — local evidence cannot pass as hosted ARM64)",
        )
        _require(
            receipt.get("previous_ref") == expected_previous_image,
            f"{label}: receipts tested previous "
            f"{receipt.get('previous_ref')!r}, not the pinned baseline",
        )
        _validate_unique_owned_resources(receipt, label)
        _validate_count_chain(receipt, label)

        if scenario == "same_digest_smoke":
            _require(
                receipt.get("candidate_ref") == receipt.get("previous_ref"),
                "same-image control: candidate ref differs from the previous ref",
            )
            _require(
                receipt.get("candidate_image_id") == receipt.get("previous_image_id"),
                "same-image control: candidate id differs from the previous id",
            )
            _require(
                receipt.get("previous_image_id") == expected_previous_id,
                f"same-image control: previous id "
                f"{receipt.get('previous_image_id')!r} is not the pinned baseline",
            )
        else:
            _require(
                receipt.get("candidate_ref") == expected_candidate_image,
                f"upgrade arm: candidate {receipt.get('candidate_ref')!r} is not "
                f"the staged {expected_candidate_image!r}",
            )
            _require(
                receipt.get("previous_image_id") == expected_previous_id,
                f"upgrade arm: previous id {receipt.get('previous_image_id')!r} "
                "is not the pinned baseline id",
            )
            _require(
                receipt.get("candidate_image_id") == expected_candidate_id,
                f"upgrade arm: candidate id {receipt.get('candidate_image_id')!r} "
                f"is not the staged {expected_candidate_id!r}",
            )
            _require(
                receipt.get("candidate_image_id") != receipt.get("previous_image_id"),
                "upgrade arm: distinct digests collapsed to one image id; no "
                "upgrade was actually exercised",
            )

    tokens = [str(r.get("run_token")) for r in records]
    if len(set(tokens)) != len(tokens):
        raise HonchoGateError(f"run tokens are reused across scenarios: {tokens}")
    workspaces = [str(r.get("workspace")) for r in records]
    if len(set(workspaces)) != len(workspaces):
        raise HonchoGateError(f"workspaces are reused across scenarios: {workspaces}")

    upgrade = by_scenario["two_version_upgrade"]
    return {
        "scenarios": sorted(by_scenario),
        "mode": expected_mode,
        "arch": expected_arch,
        "previous_image": expected_previous_image,
        "previous_id": expected_previous_id,
        "candidate_image": expected_candidate_image,
        "candidate_id": expected_candidate_id,
        "same_image_control": "bound",
        "message_ids": {
            scenario: sorted(
                str(m) for m in (
                    receipt.get("message_id"),
                    receipt.get("write_after_replace_id"),
                    receipt.get("write_after_restart_id"),
                )
                if m is not None
            )
            for scenario, receipt in by_scenario.items()
        },
        "run_tokens": tokens,
        "final_disk_messages": upgrade["disk_message_counts"][
            "disk_after_post_restart_write"
        ],
    }


def load_receipts(directory: Path | str) -> list[dict]:
    path = Path(directory)
    if not path.is_dir():
        raise HonchoGateError(f"receipt directory {path} is missing")
    receipts: list[dict] = []
    for candidate in sorted(path.glob("*.json")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise HonchoGateError(f"{candidate} is not JSON: {exc}") from exc
        if isinstance(value, dict):
            receipts.append(value)
    if not receipts:
        raise HonchoGateError(f"{path} contains no receipt JSON")
    return receipts


def summary_markdown(summary: Mapping) -> str:
    lines = [
        "## Honcho native ARM64 release qualification",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Arch | `{summary['arch']}` |",
        f"| Endpoint mode | `{summary['mode']}` |",
        f"| Previous | `{summary['previous_image']}` (`{summary['previous_id']}`) |",
        f"| Candidate | `{summary['candidate_image']}` (`{summary['candidate_id']}`) |",
        f"| Same-image control | `{summary['same_image_control']}` |",
        f"| Final on-disk messages | {summary['final_disk_messages']} |",
        f"| Run tokens | {', '.join(f'`{t}`' for t in summary['run_tokens'])} |",
        "",
        "Publishing: disabled. No registry login, public push, tag, package "
        "scope, or artifact.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    junit = sub.add_parser("junit", help="require named Docker cases, zero skips")
    junit.add_argument("report", type=Path)
    junit.add_argument("--pytest-rc", type=int, required=True)

    receipts = sub.add_parser("receipts", help="validate both honcho scenarios")
    receipts.add_argument("directory", type=Path)
    receipts.add_argument("--expected-mode", required=True)
    receipts.add_argument("--expected-arch", required=True)
    receipts.add_argument("--expected-previous", required=True)
    receipts.add_argument("--expected-previous-id", required=True)
    receipts.add_argument("--expected-candidate", required=True)
    receipts.add_argument("--expected-candidate-id", required=True)
    receipts.add_argument("--summary", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "junit":
            result = validate_junit(args.report, pytest_rc=args.pytest_rc)
        else:
            result = validate_receipts(
                load_receipts(args.directory),
                expected_mode=args.expected_mode,
                expected_arch=args.expected_arch,
                expected_previous_image=args.expected_previous,
                expected_previous_id=args.expected_previous_id,
                expected_candidate_image=args.expected_candidate,
                expected_candidate_id=args.expected_candidate_id,
            )
            if args.summary:
                args.summary.write_text(summary_markdown(result), encoding="utf-8")
    except HonchoGateError as exc:
        print(f"honcho-native gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
