from __future__ import annotations

import json
from pathlib import Path

from aio_fleet.report import (
    FLEET_REPORT_SCHEMA_VERSION,
    FleetReport,
    fleet_report_json_schema,
    stable_report_json,
    validate_report_shape,
)


def test_fleet_report_round_trips_stable_shape() -> None:
    report = FleetReport(
        generated_at="2026-05-05T00:00:00+00:00",
        issue_repo="wgross19/aio-fleet",
        warnings=[],
        summary={"posture": "green"},
        rows=[],
    ).to_state()

    assert report["schema_version"] == FLEET_REPORT_SCHEMA_VERSION  # nosec B101
    assert validate_report_shape(report) == []  # nosec B101
    assert json.loads(stable_report_json(report)) == report  # nosec B101


def test_fleet_report_schema_declares_required_contract() -> None:
    schema = fleet_report_json_schema()

    assert schema["properties"]["schema_version"]["const"] == 4  # nosec B101
    for key in (
        "summary",
        "rows",
        "actions",
        "failures",
        "approvals",
        "catalog",
        "standards",
        "candidates",
        "activity",
        "registry",
        "releases",
        "workflow",
    ):
        assert key in schema["required"]  # nosec B101


def test_fleet_report_schema_snapshot() -> None:
    snapshot = Path("tests/snapshots/fleet-report-schema-v4.json")

    assert stable_report_json(
        fleet_report_json_schema()
    ) == snapshot.read_text().rstrip(  # nosec B101
        "\n"
    )


def test_validate_report_shape_rejects_extra_top_level_keys() -> None:
    report = FleetReport(
        generated_at="2026-05-05T00:00:00+00:00",
        issue_repo="wgross19/aio-fleet",
        summary={},
        rows=[],
    ).to_state()
    report["surprise"] = True

    failures = validate_report_shape(report)

    assert "unexpected top-level key: surprise" in failures  # nosec B101
