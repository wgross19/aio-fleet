from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from aio_fleet.catalog_workflow import (
    VALIDATE_WORKFLOW,
    catalog_workflow_findings,
    render_validate_catalog_workflow,
    resolve_aio_fleet_ref,
    write_validate_catalog_workflow,
)
from aio_fleet.cli import cmd_catalog_workflow


def test_catalog_workflow_detects_stale_ref_and_retired_files(tmp_path: Path) -> None:
    workflow = tmp_path / VALIDATE_WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text(render_validate_catalog_workflow("0" * 40))
    (tmp_path / ".github" / "workflows" / "changelog.yml").write_text("name: old\n")
    (tmp_path / "cliff.toml").write_text("[changelog]\n")

    findings = catalog_workflow_findings(tmp_path, aio_fleet_ref="1" * 40)

    assert (
        f"{VALIDATE_WORKFLOW}: drifted from aio-fleet renderer" in findings
    )  # nosec B101
    assert any("changelog.yml" in finding for finding in findings)  # nosec B101
    assert any("cliff.toml" in finding for finding in findings)  # nosec B101


def test_catalog_workflow_write_repairs_validate_workflow(tmp_path: Path) -> None:
    args = Namespace(
        catalog_path=str(tmp_path),
        aio_fleet_ref="1" * 40,
        write=True,
        check=False,
    )

    assert cmd_catalog_workflow(args) == 0  # nosec B101
    assert (tmp_path / VALIDATE_WORKFLOW).read_text() == (  # nosec B101
        render_validate_catalog_workflow("1" * 40)
    )

    args.write = False
    args.check = True
    assert cmd_catalog_workflow(args) == 0  # nosec B101


def test_catalog_workflow_reports_missing_checkout(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert catalog_workflow_findings(missing, aio_fleet_ref="1" * 40) == [  # nosec B101
        f"{missing}: catalog checkout is missing"
    ]
    with pytest.raises(FileNotFoundError, match="catalog checkout is missing"):
        write_validate_catalog_workflow(missing, aio_fleet_ref="1" * 40)


def test_catalog_workflow_default_prints_rendered_workflow(
    tmp_path: Path, capsys
) -> None:
    args = Namespace(
        catalog_path=str(tmp_path),
        aio_fleet_ref="1" * 40,
        write=False,
        check=False,
    )

    assert cmd_catalog_workflow(args) == 0  # nosec B101
    assert capsys.readouterr().out == render_validate_catalog_workflow(
        "1" * 40
    )  # nosec B101


def test_catalog_workflow_rejects_unpinned_ref() -> None:
    with pytest.raises(ValueError, match="40-character lowercase commit SHA"):
        render_validate_catalog_workflow("main")


def test_resolve_aio_fleet_ref_prefers_github_sha() -> None:
    assert resolve_aio_fleet_ref(env={"GITHUB_SHA": "A" * 40}) == "a" * 40  # nosec B101


def test_resolve_aio_fleet_ref_prefers_explicit_ref() -> None:
    assert (
        resolve_aio_fleet_ref(  # nosec B101
            env={"AIO_FLEET_REF": "b" * 40, "GITHUB_SHA": "a" * 40}
        )
        == "b" * 40
    )
