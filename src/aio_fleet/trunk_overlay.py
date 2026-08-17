from __future__ import annotations

import shutil
from pathlib import Path

import yaml


def copy_trunk_overlay(central_trunk: Path, target_trunk: Path) -> None:
    target_trunk.mkdir(exist_ok=True)
    config = yaml.safe_load((central_trunk / "trunk.yaml").read_text()) or {}
    if isinstance(config, dict):
        # Fleet-managed repos use aio-fleet hooks; Trunk actions would take over
        # core.hooksPath in temporary overlays and bypass the fleet wrapper.
        config.pop("actions", None)
        target_trunk.joinpath("trunk.yaml").write_text(
            yaml.safe_dump(config, sort_keys=False)
        )
    else:
        shutil.copy2(central_trunk / "trunk.yaml", target_trunk / "trunk.yaml")

    if (central_trunk / "configs").exists():
        shutil.copytree(
            central_trunk / "configs", target_trunk / "configs", dirs_exist_ok=True
        )
