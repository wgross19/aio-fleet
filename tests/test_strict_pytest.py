"""Contract tests for the fail-closed strict pytest runner.

The runner is the only path central checks use to run app tests, so every
uncertain state must fail closed: missing/malformed/empty junit evidence,
zero collected tests, skipped tests, errors, failures, and a required-but-
unavailable Docker engine. The sanitized caller contract (``python -I
<runner script> [flags] <pytest args...>``) is exercised end to end.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - tests spawn the real local pytest runner
import sys
from pathlib import Path

import pytest
from aio_fleet import strict_pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_SCRIPT = ROOT / "src" / "aio_fleet" / "strict_pytest.py"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["pytest"], returncode, stdout=stdout, stderr=stderr
    )


def _forbidden_subprocess(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("subprocess must not run for this fail-closed input")


def _write(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _junit_path_from(command: list[str]) -> Path:
    for argument in command:
        if argument.startswith("--junitxml="):
            return Path(argument.split("=", 1)[1])
    raise AssertionError(f"command has no --junitxml argument: {command!r}")


def _fake_pytest(*, returncode: int = 0, report: str | None = None, stdout: str = ""):
    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if report is not None:
            _junit_path_from(command).write_text(report, encoding="utf-8")
        return _completed(returncode=returncode, stdout=stdout)

    return fake_run


def _evidence(result: strict_pytest.StrictRunResult) -> str:
    return "\n".join(result.evidence)


def _cli_env() -> dict[str, str]:
    return dict(os.environ)


def test_declared_green_requires_individual_case_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        strict_pytest.subprocess, "run", _fake_pytest(report='<testsuite tests="12"/>')
    )
    result = strict_pytest.run_strict_pytest(["tests"], cwd=tmp_path)
    assert not result.ok  # nosec B101


# --- junit report parsing ----------------------------------------------------


def test_parse_junit_summary_counts_all_states(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="7" errors="1" failures="2" skipped="1" deselected="3">\n'
        '  <testcase classname="a" name="one"/>\n'
        "</testsuite>\n",
        encoding="utf-8",
    )

    summary = strict_pytest.parse_junit_summary(report)

    assert summary.tests == 7
    assert summary.errors == 1
    assert summary.failures == 2
    assert summary.skipped == 1
    assert summary.deselected == 3


def test_parse_junit_summary_treats_absent_deselected_as_zero(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="4" errors="0" failures="0" skipped="0"/>\n',
        encoding="utf-8",
    )

    summary = strict_pytest.parse_junit_summary(report)

    assert summary.tests == 4
    assert summary.deselected == 0


def test_parse_junit_summary_does_not_double_count_nested_suites(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites tests="5" errors="1" failures="0" skipped="0">\n'
        '  <testsuite tests="5" errors="1" failures="0" skipped="0">\n'
        '    <testcase classname="a" name="one"/>\n'
        '    <testcase classname="a" name="two">\n'
        '      <error message="boom"/>\n'
        "    </testcase>\n"
        "  </testsuite>\n"
        "</testsuites>\n",
        encoding="utf-8",
    )

    summary = strict_pytest.parse_junit_summary(report)

    assert summary.tests == 5
    assert summary.errors == 1


def test_parse_junit_summary_derives_undercounted_states_from_testcases(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="1" errors="0" failures="0" skipped="0">\n'
        '  <testcase name="passes"/>\n'
        '  <testcase name="skips"><skipped message="later"/></testcase>\n'
        '  <testcase name="fails"><failure message="no"/></testcase>\n'
        '  <testcase name="errors"><error message="boom"/></testcase>\n'
        "</testsuite>\n",
        encoding="utf-8",
    )

    summary = strict_pytest.parse_junit_summary(report)

    assert summary.tests == 4
    assert summary.skipped == 1
    assert summary.failures == 1
    assert summary.errors == 1


def test_parse_junit_summary_accepts_an_empty_testsuites_root(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuites/>\n", encoding="utf-8")

    summary = strict_pytest.parse_junit_summary(report)

    assert summary == strict_pytest.JUnitSummary(
        tests=0, errors=0, failures=0, skipped=0, deselected=0
    )


def test_parse_junit_summary_rejects_an_empty_report(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("  \n", encoding="utf-8")

    with pytest.raises(strict_pytest.JUnitReportError, match="empty"):
        strict_pytest.parse_junit_summary(report)


def test_parse_junit_summary_rejects_malformed_xml(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text('<testsuites><testsuite tests="1"', encoding="utf-8")

    with pytest.raises(strict_pytest.JUnitReportError, match="malformed"):
        strict_pytest.parse_junit_summary(report)


def test_parse_junit_summary_rejects_a_non_junit_root(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<html><body/></html>\n", encoding="utf-8")

    with pytest.raises(strict_pytest.JUnitReportError, match="malformed"):
        strict_pytest.parse_junit_summary(report)


def test_parse_junit_summary_rejects_non_integer_counts(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text('<testsuite tests="many"/>\n', encoding="utf-8")

    with pytest.raises(strict_pytest.JUnitReportError, match="tests"):
        strict_pytest.parse_junit_summary(report)


def test_parse_junit_summary_rejects_negative_counts(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text('<testsuite tests="2" skipped="-1"/>\n', encoding="utf-8")

    with pytest.raises(strict_pytest.JUnitReportError, match="skipped"):
        strict_pytest.parse_junit_summary(report)


def test_deselected_from_terminal_reads_pytest_summary() -> None:
    assert (
        strict_pytest.deselected_from_terminal(
            "1 passed, 1 skipped, 1 deselected, 1 warning in 0.01s"
        )
        == 1
    )
    assert strict_pytest.deselected_from_terminal("2 passed in 0.01s") == 0


# --- command construction ----------------------------------------------------


def test_build_pytest_command_appends_junit_report() -> None:
    command = strict_pytest.build_pytest_command(
        ["tests", "-m", "not integration"], Path("aio/junit.xml")
    )

    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert command[3:-1] == ["tests", "-m", "not integration"]
    assert command[-1] == "--junitxml=aio/junit.xml"


def test_build_pytest_command_keeps_junit_flag_before_a_passthrough_separator() -> None:
    command = strict_pytest.build_pytest_command(
        ["-q", "--", "test_green.py"], Path("aio/junit.xml")
    )

    assert command == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml=aio/junit.xml",
        "--",
        "test_green.py",
    ]


def test_build_pytest_command_preserves_pytest_arguments_exactly() -> None:
    args = ["tests", "-m", "not integration", "-k", "smoke or gate", "--timeout=90"]

    command = strict_pytest.build_pytest_command(args, Path("aio/junit.xml"))

    assert command[3 : 3 + len(args)] == args


# --- runner flag routing -----------------------------------------------------


def test_runner_flags_never_leak_into_the_pytest_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return _completed(returncode=5)

    monkeypatch.setattr(strict_pytest.subprocess, "run", fake_run)
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: True)

    exit_code = strict_pytest.run(
        [
            "--aio-strict-require-docker",
            "--aio-strict-timeout-seconds",
            "300",
            "-q",
            "test_green.py",
        ]
    )

    assert exit_code == 1
    assert captured[:3] == [sys.executable, "-m", "pytest"]
    assert captured[3:-1] == ["-q", "test_green.py"]
    assert captured[-1].startswith("--junitxml=")


def test_timeout_flag_supports_equals_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return _completed(returncode=5)

    monkeypatch.setattr(strict_pytest.subprocess, "run", fake_run)

    exit_code = strict_pytest.run(
        ["--aio-strict-timeout-seconds=45", "-q", "test_green.py"]
    )

    assert exit_code == 1
    assert "--aio-strict-timeout-seconds=45" not in captured
    assert "45" not in captured


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["--aio-strict-timeout-seconds", "soon", "tests"], "integer"),
        (["--aio-strict-timeout-seconds", "0", "tests"], "greater than zero"),
        (["--aio-strict-timeout-seconds"], "integer"),
        ([], "empty-arguments"),
        (["--aio-strict-require-docker"], "empty-arguments"),
    ],
)
def test_bad_runner_arguments_fail_closed_without_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    fragment: str,
) -> None:
    monkeypatch.setattr(strict_pytest.subprocess, "run", _forbidden_subprocess)

    exit_code = strict_pytest.run(argv)

    assert exit_code == 1
    assert fragment in capsys.readouterr().out


def test_empty_selection_fails_closed_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest.subprocess, "run", _forbidden_subprocess)

    result = strict_pytest.run_strict_pytest([], cwd=tmp_path)

    assert result.ok is False
    assert "empty-selection" in _evidence(result)


# --- green, passing, deselected and empty suites -----------------------------


def test_green_suite_passes_with_evidence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_green.py",
        "def test_one() -> None:\n    assert True\n\n\n"
        "def test_two() -> None:\n    assert 1 == 1\n",
    )

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is True
    assert result.returncode == 0
    evidence = _evidence(result)
    assert "strict-pytest: ok" in evidence
    assert "collected=2" in evidence
    assert "deselected=0" in evidence
    assert "passed=2" in evidence
    assert "skipped=0" in evidence


def test_deselected_tests_are_allowed_when_the_rest_pass(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_mixed.py",
        "import pytest\n\n\n"
        "def test_unit() -> None:\n    assert True\n\n\n"
        "@pytest.mark.integration\n"
        "def test_integration() -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(
        ["-m", "not integration", "test_mixed.py"], cwd=tmp_path
    )

    assert result.ok is True
    evidence = _evidence(result)
    assert "collected=1" in evidence
    assert "deselected=1" in evidence
    assert "passed=1" in evidence
    assert "skipped=0" in evidence


def test_deselecting_every_test_is_an_empty_suite(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_mixed.py",
        "import pytest\n\n\n"
        "@pytest.mark.integration\n"
        "def test_integration() -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(
        ["-m", "not integration", "test_mixed.py"], cwd=tmp_path
    )

    assert result.ok is False
    assert result.returncode == 5
    assert "empty-suite" in _evidence(result)


def test_no_tests_collected_is_an_empty_suite(tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()

    result = strict_pytest.run_strict_pytest(["nothing"], cwd=tmp_path)

    assert result.ok is False
    assert "empty-suite" in _evidence(result)


# --- fail-closed test states -------------------------------------------------


def test_skipped_suite_fails_closed_even_on_zero_exit(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_skip.py",
        "import pytest\n\n\n"
        "def test_skipped() -> None:\n"
        "    pytest.skip('exercise strict skip handling')\n\n\n"
        "def test_passes() -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(["test_skip.py"], cwd=tmp_path)

    assert result.ok is False
    assert result.returncode == 0
    evidence = _evidence(result)
    assert "FAIL skipped-tests" in evidence
    assert "skipped=1" in evidence


def test_xfail_is_reported_as_a_skip_and_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_xfail.py",
        "import pytest\n\n\n"
        "@pytest.mark.xfail(reason='known gap')\n"
        "def test_known_gap() -> None:\n    assert False\n",
    )

    result = strict_pytest.run_strict_pytest(["test_xfail.py"], cwd=tmp_path)

    assert result.ok is False
    assert "skipped-tests" in _evidence(result)


def test_module_level_skip_is_reported_as_skipped_not_empty(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_modskip.py",
        "import pytest\n\n"
        "pytest.skip('whole module off', allow_module_level=True)\n\n\n"
        "def test_never() -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(["test_modskip.py"], cwd=tmp_path)

    assert result.ok is False
    assert result.returncode == 5
    evidence = _evidence(result)
    assert "skipped-tests" in evidence
    assert "skipped=1" in evidence


def test_failing_suite_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_fails.py",
        "def test_broken() -> None:\n    assert False\n",
    )

    result = strict_pytest.run_strict_pytest(["test_fails.py"], cwd=tmp_path)

    assert result.ok is False
    assert "pytest-exit" in _evidence(result)
    assert "failures=1" in _evidence(result)


def test_erroring_suite_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_error.py",
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def broken() -> None:\n    raise RuntimeError('boom')\n\n\n"
        "def test_error(broken: None) -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(["test_error.py"], cwd=tmp_path)

    assert result.ok is False
    assert "errors=1" in _evidence(result)


def test_collection_error_fails_closed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_collect_error.py",
        "import nonexistent_module_for_strict_pytest\n\n\n"
        "def test_never_runs() -> None:\n    assert True\n",
    )

    result = strict_pytest.run_strict_pytest(["test_collect_error.py"], cwd=tmp_path)

    assert result.ok is False
    assert result.returncode != 0
    assert "pytest-exit" in _evidence(result)


# --- junit evidence fail-closed states ---------------------------------------


def test_missing_junit_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest.subprocess, "run", _fake_pytest())

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is False
    assert "junit-missing" in _evidence(result)


def test_empty_junit_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest.subprocess, "run", _fake_pytest(report="  \n"))

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is False
    assert "junit-empty" in _evidence(result)


def test_malformed_junit_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        strict_pytest.subprocess, "run", _fake_pytest(report="<testsuites")
    )

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is False
    assert "junit-malformed" in _evidence(result)


def test_zero_test_junit_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        strict_pytest.subprocess,
        "run",
        _fake_pytest(
            report='<testsuites><testsuite tests="0" errors="0" '
            'failures="0" skipped="0"/></testsuites>'
        ),
    )

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is False
    assert "empty-suite" in _evidence(result)


def test_junit_skip_is_authoritative_even_when_pytest_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        strict_pytest.subprocess,
        "run",
        _fake_pytest(
            report='<testsuites><testsuite tests="1" errors="0" failures="0" '
            'skipped="1"><testcase name="skipped"><skipped/></testcase>'
            "</testsuite></testsuites>"
        ),
    )

    result = strict_pytest.run_strict_pytest(["test_skip.py"], cwd=tmp_path)

    assert result.ok is False
    assert "skipped=1" in _evidence(result)


# --- timeout and Docker gating ------------------------------------------------


def test_inner_pytest_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(
            cmd=["pytest"], timeout=timeout if isinstance(timeout, int) else 0
        )

    monkeypatch.setattr(strict_pytest.subprocess, "run", fake_run)

    result = strict_pytest.run_strict_pytest(
        ["test_slow.py"], cwd=tmp_path, timeout_seconds=5
    )

    assert result.ok is False
    assert "pytest-timeout" in _evidence(result)


def test_required_docker_unavailable_fails_before_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: False)
    monkeypatch.setattr(strict_pytest.subprocess, "run", _forbidden_subprocess)

    result = strict_pytest.run_strict_pytest(
        ["tests/integration"], cwd=tmp_path, require_docker=True
    )

    assert result.ok is False
    assert "docker-required-unavailable" in _evidence(result)


def test_unavailable_docker_does_not_block_non_docker_suites(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: False)
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is True


def test_available_docker_allows_required_docker_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: True)
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    result = strict_pytest.run_strict_pytest(
        ["test_green.py"], cwd=tmp_path, require_docker=True
    )

    assert result.ok is True


def test_docker_probe_without_binary_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strict_pytest.shutil, "which", lambda _name: None)
    monkeypatch.setattr(strict_pytest.subprocess, "run", _forbidden_subprocess)

    assert strict_pytest.docker_available() is False


def test_docker_probe_requires_a_healthy_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strict_pytest.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        strict_pytest.subprocess, "run", lambda *_a, **_k: _completed(returncode=1)
    )

    assert strict_pytest.docker_available() is False


def test_docker_probe_hanging_daemon_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hanging_run(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=30)

    monkeypatch.setattr(strict_pytest.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(strict_pytest.subprocess, "run", hanging_run)

    assert strict_pytest.docker_available() is False


def test_docker_probe_permission_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_run(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("docker socket denied")

    monkeypatch.setattr(strict_pytest.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(strict_pytest.subprocess, "run", failing_run)

    assert strict_pytest.docker_available() is False


def test_docker_probe_uses_info_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(returncode=0)

    monkeypatch.setattr(strict_pytest.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(strict_pytest.subprocess, "run", fake_run)

    assert strict_pytest.docker_available() is True
    assert commands == [["/usr/bin/docker", "info"]]


# --- runner report lifecycle and environment ---------------------------------


def test_default_junit_report_is_cleaned_up(tmp_path: Path) -> None:
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    result = strict_pytest.run_strict_pytest(["test_green.py"], cwd=tmp_path)

    assert result.ok is True
    assert result.junit_path is not None
    assert result.junit_path.exists() is False


def test_caller_supplied_junit_report_is_preserved(tmp_path: Path) -> None:
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")
    report = tmp_path / "requested-junit.xml"

    result = strict_pytest.run_strict_pytest(
        ["test_green.py"], cwd=tmp_path, junit_path=report
    )

    assert result.ok is True
    assert result.junit_path == report
    assert report.exists() is True


def test_sanitized_environment_is_forwarded_to_pytest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    env = {"PATH": "/usr/bin", "HOME": str(tmp_path)}

    def fake_run(
        _command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return _completed(returncode=5)

    monkeypatch.setattr(strict_pytest.subprocess, "run", fake_run)

    strict_pytest.run_strict_pytest(["tests"], cwd=tmp_path, env=env)

    assert captured["env"] == env
    assert captured["cwd"] == tmp_path


# --- sanitized caller CLI ----------------------------------------------------


def test_sanitized_caller_green_suite_exits_zero(tmp_path: Path) -> None:
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    completed = subprocess.run(  # nosec B603 - exact caller contract under test
        [sys.executable, "-I", str(RUNNER_SCRIPT), "-q", "test_green.py"],
        cwd=tmp_path,
        env=_cli_env(),
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert completed.returncode == 0
    assert "strict-pytest: ok" in completed.stdout
    assert "collected=1" in completed.stdout
    assert "deselected=0" in completed.stdout


def test_sanitized_caller_honors_pytest_marker_args(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_mixed.py",
        "import pytest\n\n\n"
        "def test_unit() -> None:\n    assert True\n\n\n"
        "@pytest.mark.integration\n"
        "def test_integration() -> None:\n    assert True\n",
    )

    completed = subprocess.run(  # nosec B603 - exact caller contract under test
        [
            sys.executable,
            "-I",
            str(RUNNER_SCRIPT),
            "-q",
            "-m",
            "not integration",
            "test_mixed.py",
        ],
        cwd=tmp_path,
        env=_cli_env(),
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert completed.returncode == 0
    assert "collected=1" in completed.stdout
    assert "deselected=1" in completed.stdout


def test_sanitized_caller_skipped_suite_exits_nonzero(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_skip.py",
        "import pytest\n\n\n"
        "def test_skipped() -> None:\n"
        "    pytest.skip('exercise strict skip handling')\n",
    )

    completed = subprocess.run(  # nosec B603 - exact caller contract under test
        [sys.executable, "-I", str(RUNNER_SCRIPT), "-q", "test_skip.py"],
        cwd=tmp_path,
        env=_cli_env(),
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert completed.returncode == 1
    assert "FAIL skipped-tests" in completed.stdout
    assert "skipped=1" in completed.stdout


def test_sanitized_caller_timeout_flag_is_consumed(tmp_path: Path) -> None:
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    completed = subprocess.run(  # nosec B603 - exact caller contract under test
        [
            sys.executable,
            "-I",
            str(RUNNER_SCRIPT),
            "--aio-strict-timeout-seconds",
            "300",
            "-q",
            "test_green.py",
        ],
        cwd=tmp_path,
        env=_cli_env(),
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert completed.returncode == 0


def test_cli_require_docker_fails_before_pytest_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: False)
    monkeypatch.setattr(strict_pytest.subprocess, "run", _forbidden_subprocess)

    exit_code = strict_pytest.run(
        ["--aio-strict-require-docker", "-q", "tests/integration"]
    )

    assert exit_code == 1


def test_cli_require_docker_runs_green_suite_with_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(strict_pytest, "docker_available", lambda: True)
    _write(tmp_path, "test_green.py", "def test_ok() -> None:\n    assert True\n")

    exit_code = strict_pytest.run(
        ["--aio-strict-require-docker", "-q", "test_green.py"]
    )

    assert exit_code == 0
