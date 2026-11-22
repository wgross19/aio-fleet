"""Fail-closed pytest runner for central checks.

Central checks must never turn a required test suite into a silent pass. This
runner enforces:

- pytest must be importable with the interpreter running the suite; a missing
  pytest fails closed instead of producing an empty pass;
- docker-backed suites require a working Docker engine; a missing, unhealthy,
  or hanging Docker fails closed instead of letting the suite skip itself;
- every collected test that pytest reports skipped fails the suite even when
  pytest exits 0 (this includes xfail-style skips, which junit reports as
  skipped);
- a suite that collects zero tests fails, including a selection that
  deselects every test;
- a missing, empty, unreadable, or malformed junit report fails closed;
- failing or erroring tests fail; only strictly green suites pass.

The runner executes pytest as a child process with ``--junitxml`` pointed at a
report it owns and parses that report for the authoritative skip/error/empty
evidence. Marker-based deselection (``-m "not integration"``) is expected and
allowed; skipped tests are not. Deselected counts are read from the pytest
terminal summary because pytest's junit report does not record them.

CLI: ``python -m aio_fleet.strict_pytest [--aio-strict-require-docker]
[--aio-strict-timeout-seconds N] <pytest args...>``. The runner flags are
consumed here and never forwarded to pytest. The sanitized caller invokes the
file directly as ``python -I <path>/strict_pytest.py ...``, so this module
imports only the standard library plus the installed ``defusedxml``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess  # nosec B404 - runs only the trusted local pytest entry point
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# Type-only: stdlib Element type for annotations; all XML parsing goes through
# defusedxml above (repo-wide XML policy over our own private junit report).
from xml.etree.ElementTree import Element  # nosec B405 - type alias only, never parses

from defusedxml import ElementTree as _DefusedET

RUNNER_DOCKER_FLAG = "--aio-strict-require-docker"
RUNNER_TIMEOUT_FLAG = "--aio-strict-timeout-seconds"

_DOCKER_PROBE_TIMEOUT_SECONDS = 30
_EVIDENCE_PREFIX = "strict-pytest:"
_COUNT_ATTRIBUTES = ("tests", "errors", "failures", "skipped", "deselected")
_DESELECTED_PATTERN = re.compile(r"(?<![\w.])(\d+)\s+deselected\b")


class JUnitReportError(ValueError):
    """A junit report that cannot be trusted; ``reason`` names the state."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class JUnitSummary:
    tests: int
    errors: int
    failures: int
    skipped: int
    deselected: int
    cases: int = 0


@dataclass(frozen=True)
class StrictRunResult:
    ok: bool
    evidence: list[str]
    returncode: int
    junit_path: Path | None


def parse_junit_summary(path: Path) -> JUnitSummary:
    """Parse a junit report, never under-counting skipped/error/failing tests."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JUnitReportError("missing", f"junit report {path} is missing") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise JUnitReportError(
            "malformed", f"junit report {path} is malformed: could not read it: {exc}"
        ) from exc
    if not text.strip():
        raise JUnitReportError("empty", f"junit report {path} is empty")
    try:
        root = _DefusedET.fromstring(text)
    except (SyntaxError, ValueError) as exc:
        # ParseError subclasses SyntaxError; DefusedXmlException subclasses ValueError.
        raise JUnitReportError(
            "malformed", f"junit report {path} is malformed: {exc}"
        ) from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JUnitReportError(
            "malformed",
            f"junit report {path} is malformed: root element <{root.tag}> "
            "is not a test suite",
        )

    suites = _leaf_testsuites(root)
    if not suites:
        if "tests" not in root.attrib:
            return JUnitSummary(tests=0, errors=0, failures=0, skipped=0, deselected=0)
        suites = [root]

    counts = [_suite_counts(suite) for suite in suites]
    return JUnitSummary(
        tests=sum(entry["tests"] for entry in counts),
        errors=sum(entry["errors"] for entry in counts),
        failures=sum(entry["failures"] for entry in counts),
        skipped=sum(entry["skipped"] for entry in counts),
        deselected=sum(entry["deselected"] for entry in counts),
        cases=sum(1 for _ in root.iter("testcase")),
    )


def _leaf_testsuites(root: Element) -> list[Element]:
    """Return innermost suites so wrapper aggregates are not counted twice."""
    suites = [element for element in root.iter() if element.tag == "testsuite"]
    leaves = [
        suite
        for suite in suites
        if not any(
            child.tag == "testsuite" for child in suite.iter() if child is not suite
        )
    ]
    return leaves or suites


def _suite_counts(suite: Element) -> dict[str, int]:
    declared = {name: _int_attribute(suite, name) for name in _COUNT_ATTRIBUTES}
    cases = [child for child in suite if child.tag == "testcase"]
    derived = {
        "tests": len(cases),
        "errors": sum(1 for case in cases if case.find("error") is not None),
        "failures": sum(1 for case in cases if case.find("failure") is not None),
        "skipped": sum(1 for case in cases if case.find("skipped") is not None),
        "deselected": 0,
    }
    # Fail closed: a report whose attributes under-count its own testcase
    # elements must not be trusted to show a clean suite.
    return {name: max(declared[name], derived[name]) for name in _COUNT_ATTRIBUTES}


def _int_attribute(element: Element, name: str) -> int:
    raw = element.get(name)
    if raw is None or not raw.strip():
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise JUnitReportError(
            "malformed", f"junit attribute {name}={raw!r} is not an integer"
        ) from exc
    if value < 0:
        raise JUnitReportError(
            "malformed", f"junit attribute {name}={raw!r} is negative"
        )
    return value


def deselected_from_terminal(output: str) -> int:
    """Read the deselected count from pytest's terminal summary, if present."""
    return max(
        (int(match.group(1)) for match in _DESELECTED_PATTERN.finditer(output)),
        default=0,
    )


def build_pytest_command(pytest_args: Sequence[str], junit_xml: Path) -> list[str]:
    args = list(pytest_args)
    junit = [f"--junitxml={junit_xml}"]
    if "--" in args:
        # Keep pytest's option separator last; anything appended after it would
        # be treated as a test path instead of the junit option.
        index = args.index("--")
        return [sys.executable, "-m", "pytest", *args[:index], *junit, *args[index:]]
    return [sys.executable, "-m", "pytest", *args, *junit]


def docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(  # nosec B603
            [docker, "info"],
            check=False,
            text=True,
            capture_output=True,
            timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def pytest_importable() -> bool:
    try:
        return importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError):
        return False


def run_strict_pytest(
    pytest_args: Sequence[str],
    *,
    cwd: Path | str,
    env: dict[str, str] | None = None,
    require_docker: bool = False,
    timeout_seconds: int | None = None,
    junit_path: Path | None = None,
) -> StrictRunResult:
    """Run pytest and fail closed unless the suite is strictly green.

    A report created by this call is removed afterwards. A caller-supplied
    ``junit_path`` is left in place, since the caller asked for that artifact.
    """
    cwd = Path(cwd)
    evidence: list[str] = []

    def _closed(
        reason: str, detail: str, *, returncode: int, junit: Path | None
    ) -> StrictRunResult:
        evidence.append(f"{_EVIDENCE_PREFIX} FAIL {reason}: {detail}")
        return StrictRunResult(
            ok=False, evidence=evidence, returncode=returncode, junit_path=junit
        )

    if not list(pytest_args):
        return _closed(
            "empty-selection",
            "no pytest selection was given; failing closed instead of running "
            "an unintended default suite.",
            returncode=2,
            junit=None,
        )
    if require_docker and not docker_available():
        return _closed(
            "docker-required-unavailable",
            "Docker is required for this suite and is not usable here; "
            "failing closed instead of skipping.",
            returncode=1,
            junit=None,
        )
    if not pytest_importable():
        return _closed(
            "pytest-missing",
            "pytest is not importable with this interpreter; "
            "failing closed instead of skipping.",
            returncode=1,
            junit=None,
        )

    owned_dir: Path | None = None
    if junit_path is None:
        owned_dir = Path(tempfile.mkdtemp(prefix="aio-strict-pytest-"))
        report_path = owned_dir / "junit.xml"
    else:
        report_path = Path(junit_path)
        report_path.unlink(
            missing_ok=True
        )  # Require a fresh report from this invocation.

    try:
        try:
            result = subprocess.run(  # nosec B603
                build_pytest_command(pytest_args, report_path),
                cwd=cwd,
                env=env,
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                print(_text(exc.stdout), end="")
            if exc.stderr:
                print(_text(exc.stderr), end="", file=sys.stderr)
            return _closed(
                "pytest-timeout",
                f"pytest exceeded {timeout_seconds or 0}s and was stopped.",
                returncode=1,
                junit=report_path,
            )

        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")

        summary: JUnitSummary | None = None
        junit_problem = ""
        junit_detail = ""
        if not report_path.exists():
            junit_problem = "junit-missing"
            junit_detail = (
                f"pytest exited {result.returncode} but wrote no junit report "
                f"at {report_path}; failing closed."
            )
        else:
            try:
                summary = parse_junit_summary(report_path)
            except JUnitReportError as exc:
                junit_problem = f"junit-{exc.reason}"
                junit_detail = f"{exc}; failing closed."

        if summary is not None:
            reported = deselected_from_terminal(result.stdout or "")
            if reported > summary.deselected:
                summary = replace(summary, deselected=reported)

        def counts() -> str:
            if summary is None:
                return ""
            return (
                f" collected={summary.tests} skipped={summary.skipped}"
                f" failures={summary.failures} errors={summary.errors}"
            )

        if result.returncode == 5:
            if summary is not None and summary.skipped > 0:
                return _closed(
                    "skipped-tests",
                    f"collected={summary.tests} skipped={summary.skipped} "
                    f"failures={summary.failures} errors={summary.errors}; "
                    "the selection only collected skipped tests, which fail "
                    "closed instead of skipping.",
                    returncode=5,
                    junit=report_path,
                )
            return _closed(
                "empty-suite",
                "pytest collected 0 tests for the required selection; failing "
                f"closed instead of passing an empty suite.{counts()}",
                returncode=5,
                junit=report_path,
            )
        if result.returncode != 0:
            detail = (
                f"pytest exited {result.returncode} before strict checks passed."
                f"{counts()}"
            )
            if junit_problem:
                detail += f" ({junit_problem})"
            return _closed(
                "pytest-exit",
                detail,
                returncode=result.returncode,
                junit=report_path,
            )
        if junit_problem:
            return _closed(
                junit_problem,
                junit_detail,
                returncode=result.returncode,
                junit=report_path,
            )
        if summary is None:
            return _closed(
                "junit-missing",
                f"pytest exited 0 but no junit counts are available at "
                f"{report_path}; failing closed.",
                returncode=result.returncode,
                junit=report_path,
            )
        if summary.tests == 0:
            return _closed(
                "empty-suite",
                "junit report shows 0 collected tests; failing closed instead "
                f"of passing an empty suite.{counts()}",
                returncode=result.returncode,
                junit=report_path,
            )
        if summary.skipped > 0:
            return _closed(
                "skipped-tests",
                f"collected={summary.tests} skipped={summary.skipped} "
                f"failures={summary.failures} errors={summary.errors}; "
                "skipped tests fail closed instead of skipping.",
                returncode=result.returncode,
                junit=report_path,
            )
        if summary.failures > 0:
            return _closed(
                "test-failures",
                f"collected={summary.tests} skipped={summary.skipped} "
                f"failures={summary.failures} errors={summary.errors}; "
                "failing tests fail closed.",
                returncode=result.returncode,
                junit=report_path,
            )
        if summary.errors > 0:
            return _closed(
                "test-errors",
                f"collected={summary.tests} skipped={summary.skipped} "
                f"failures={summary.failures} errors={summary.errors}; "
                "errored tests fail closed.",
                returncode=result.returncode,
                junit=report_path,
            )

        if summary.cases != summary.tests:
            return _closed(
                "junit-incomplete",
                f"expected {summary.tests} testcase rows, found {summary.cases}",
                returncode=1,
                junit=report_path,
            )
        passed = summary.tests - summary.failures - summary.errors - summary.skipped
        evidence.append(
            f"{_EVIDENCE_PREFIX} ok collected={summary.tests} "
            f"deselected={summary.deselected} passed={passed} "
            f"failures={summary.failures} errors={summary.errors} "
            f"skipped={summary.skipped}"
        )
        return StrictRunResult(
            ok=True, evidence=evidence, returncode=0, junit_path=report_path
        )
    finally:
        if owned_dir is not None:
            shutil.rmtree(owned_dir, ignore_errors=True)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_runner_argv(argv: Sequence[str]) -> tuple[bool, int | None, list[str], str]:
    """Split runner flags from pytest args; return (docker, timeout, args, error)."""
    require_docker = False
    timeout_seconds: int | None = None
    pytest_args: list[str] = []
    tokens = list(argv)
    index = 0
    while index < len(tokens):
        argument = tokens[index]
        if argument == "--":
            # Everything after the separator belongs to pytest verbatim.
            pytest_args.extend(tokens[index:])
            break
        if argument == RUNNER_DOCKER_FLAG:
            require_docker = True
            index += 1
            continue
        if argument == RUNNER_TIMEOUT_FLAG or argument.startswith(
            RUNNER_TIMEOUT_FLAG + "="
        ):
            if argument == RUNNER_TIMEOUT_FLAG:
                index += 1
                if index >= len(tokens):
                    return (
                        require_docker,
                        None,
                        pytest_args,
                        (f"{RUNNER_TIMEOUT_FLAG} requires an integer second value"),
                    )
                raw = tokens[index]
            else:
                raw = argument.split("=", 1)[1]
            try:
                timeout_seconds = int(raw)
            except ValueError:
                return (
                    require_docker,
                    None,
                    pytest_args,
                    (
                        f"{RUNNER_TIMEOUT_FLAG} requires an integer second value, "
                        f"got {raw!r}"
                    ),
                )
            if timeout_seconds <= 0:
                return (
                    require_docker,
                    None,
                    pytest_args,
                    (f"{RUNNER_TIMEOUT_FLAG} must be greater than zero, got {raw!r}"),
                )
            index += 1
            continue
        pytest_args.append(argument)
        index += 1
    return require_docker, timeout_seconds, pytest_args, ""


def run(argv: Sequence[str] | None = None) -> int:
    require_docker, timeout_seconds, pytest_args, error = _parse_runner_argv(
        sys.argv[1:] if argv is None else argv
    )
    if error:
        print(f"{_EVIDENCE_PREFIX} FAIL bad-arguments: {error}")
        return 1
    if not pytest_args:
        print(
            f"{_EVIDENCE_PREFIX} FAIL empty-arguments: no pytest selection given; "
            "failing closed instead of running an empty suite."
        )
        return 1

    result = run_strict_pytest(
        pytest_args,
        cwd=Path.cwd(),
        require_docker=require_docker,
        timeout_seconds=timeout_seconds,
    )
    for line in result.evidence:
        print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
