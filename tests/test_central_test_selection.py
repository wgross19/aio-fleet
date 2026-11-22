from pathlib import Path

import pytest

from aio_fleet.control_plane import central_check_steps
from aio_fleet.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("app", ["gbrain-aio", "honcho-aio", "fastcrw-aio"])
@pytest.mark.parametrize("publish", [False, True])
def test_app_test_contract(app, publish):
    repo = load_manifest(ROOT / "fleet.yml").repo(app)
    steps = central_check_steps(
        repo, event="push" if publish else "pull_request", publish=publish
    )
    by_name = {step.name: step for step in steps}
    for name in ("unit-tests", "integration-tests"):
        step = by_name[name]
        assert step.inherit_secrets is False
        assert "-I" in step.command
        assert str(ROOT / "src/aio_fleet/strict_pytest.py") in step.command
        assert ("--aio-strict-require-docker" in step.command) == (
            name == "integration-tests"
        )
    assert {
        "tests",
        "--ignore=tests/integration",
        "--ignore=tests/release",
        "not integration",
    } <= set(by_name["unit-tests"].command)
    assert "tests/integration" in by_name["integration-tests"].command
    if publish:
        assert list(by_name).index("integration-tests") < list(by_name).index(
            "registry-publish"
        )
