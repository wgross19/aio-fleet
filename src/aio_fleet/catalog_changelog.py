from __future__ import annotations

import re
import subprocess  # nosec B404
from collections import defaultdict
from pathlib import Path

HEADER = """# Changelog

This repository tracks published Unraid template and icon updates for the
wgross19 AIO app fleet.

Notable changes are typically driven by:

- new app templates being added
- icon updates
- metadata or template corrections
- sync and maintenance cleanup

For app-specific release history, use the individual AIO repositories.
Some high-volume maintenance commits may be omitted here so this file stays readable.

This catalog is maintained continuously on `main` and does not require formal GitHub Releases.
"""

GROUP_ORDER = [
    "CI",
    "Dependency Updates",
    "Documentation",
    "Features",
    "Fixes",
    "Maintenance",
    "Performance",
    "Refactors",
    "Reverts",
]

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]+\))?!?:\s*(?P<description>.+)$"
)
PR_SUFFIX_RE = re.compile(r"\s+\(#\d+\)$")


def render_catalog_changelog(catalog_path: Path) -> str:
    groups: dict[str, list[str]] = defaultdict(list)
    for subject in _commit_subjects(catalog_path):
        parsed = _parse_subject(subject)
        if parsed is None:
            continue
        group, description = parsed
        groups[group].append(_upper_first(description))

    lines = [HEADER.rstrip(), "", "## Unreleased", ""]
    for group in GROUP_ORDER:
        commits = groups.get(group, [])
        if not commits:
            continue
        lines.extend([f"### {group}", ""])
        lines.extend(f"- {message}" for message in commits)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def catalog_changelog_drift(catalog_path: Path) -> bool:
    changelog = catalog_path / "CHANGELOG.md"
    return not changelog.exists() or changelog.read_text() != render_catalog_changelog(
        catalog_path
    )


def _commit_subjects(catalog_path: Path) -> list[str]:
    result = subprocess.run(  # nosec B603
        ["git", "log", "--reverse", "--pretty=format:%s"],
        cwd=catalog_path,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "unable to read catalog git log")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _parse_subject(subject: str) -> tuple[str, str] | None:
    subject = PR_SUFFIX_RE.sub("", subject.strip())
    if subject.startswith(("Merge pull request", "Merge branch 'main' into ")):
        return None
    if subject.startswith("chore(changelog):"):
        return None
    if re.match(r"^chore: auto-sync .* from upstream$", subject):
        return None

    match = CONVENTIONAL_RE.match(subject)
    if match:
        commit_type = match.group("type")
        description = match.group("description").strip()
        if commit_type == "chore" and subject.startswith("chore(deps"):
            return "Dependency Updates", description
        group = {
            "feat": "Features",
            "fix": "Fixes",
            "perf": "Performance",
            "refactor": "Refactors",
            "docs": "Documentation",
            "doc": "Documentation",
            "ci": "CI",
            "chore": "Maintenance",
            "revert": "Reverts",
        }.get(commit_type)
        if group:
            return group, description

    if subject.startswith("Fix:"):
        return "Fixes", subject.split(":", 1)[1].strip()
    return None


def _upper_first(value: str) -> str:
    if not value:
        return value
    return value[0].upper() + value[1:]
