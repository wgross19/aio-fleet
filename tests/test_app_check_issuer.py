"""Prevent a stale fleet App ID from making all application PRs unmergeable."""

from pathlib import Path

import yaml


def test_live_app_gate_uses_the_verified_fleet_check_producer():
    root = Path(__file__).resolve().parents[1]
    policy = yaml.safe_load((root / "infra/github/github-policy.yml").read_text())
    for name in ("gbrain-aio", "honcho-aio", "fastcrw-aio"):
        app = policy["repositories"][name]
        assert "aio-fleet / required" in app["required_checks"]
        assert app["required_check_app_id"] == 4643060, name
