from __future__ import annotations

import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from aio_fleet.manifest import RepoConfig

RETIRED_SHARED_PATHS: dict[str, str] = {
    ".github/workflows": "app workflows are replaced by aio-fleet check orchestration",
    ".github/dependabot.yml": "dependency updates run from aio-fleet Renovate",
    ".github/dependabot.yaml": "dependency updates run from aio-fleet Renovate",
    ".github/renovate.json": "shared Renovate policy runs from aio-fleet",
    ".github/renovate.json5": "shared Renovate policy runs from aio-fleet",
    ".trunk": "Trunk config runs from aio-fleet scratch checkouts",
    "cliff.toml": "git-cliff config is generated centrally",
    "renovate.json": "shared Renovate policy runs from aio-fleet",
    "renovate.json5": "shared Renovate policy runs from aio-fleet",
    "requirements-dev.txt": "shared test dependencies install from aio-fleet",
    "upstream.toml": "upstream provider state moves to .aio-fleet.yml",
    "components.toml": "component metadata moves to .aio-fleet.yml",
    "scripts/release.py": "release helpers are centralized",
    "scripts/update-template-changes.py": "XML Changes rendering is centralized",
    "scripts/check-upstream.py": "upstream monitoring is centralized",
    "scripts/validate-derived-repo.sh": "derived repo validation is centralized",
    "scripts/validate-template.py": "template validation is centralized",
    "scripts/components.py": "component metadata is read from .aio-fleet.yml",
    ".github/FUNDING.yml": "shared funding metadata is centralized outside app repos",
    ".github/ISSUE_TEMPLATE": "shared issue templates are centralized outside app repos",
    ".github/pull_request_template.md": "shared PR template is centralized outside app repos",
    "SECURITY.md": "shared security policy is centralized outside app repos",
    "tests/template/test_update_template_changes.py": "XML Changes tests move to aio-fleet",
    "tests/template/test_validate_derived_repo.py": "derived repo policy tests move to aio-fleet",
    "tests/template/test_validate_template.py": "template validator tests move to aio-fleet",
    "tests/unit/test_check_upstream.py": "upstream check tests move to aio-fleet",
    "tests/unit/test_components.py": "component metadata tests move to aio-fleet",
    "tests/unit/test_release_shim.py": "release helper tests move to aio-fleet",
}


@dataclass(frozen=True)
class CleanupFinding:
    path: Path
    reason: str
    provenance: str = "remote-confirmed"


def cleanup_findings(repo: RepoConfig) -> list[CleanupFinding]:
    findings: list[CleanupFinding] = []
    for relative, reason in RETIRED_SHARED_PATHS.items():
        if _retired_path_is_manifest_owned(repo, relative):
            continue
        path = repo.path / relative
        if path.exists():
            findings.append(
                CleanupFinding(
                    path=path,
                    reason=reason,
                    provenance=(
                        "remote-confirmed"
                        if _retired_path_has_tracked_content(repo.path, relative)
                        else "local-only"
                    ),
                )
            )
    return findings


def _retired_path_is_manifest_owned(repo: RepoConfig, relative: str) -> bool:
    if relative != "upstream.toml":
        return False

    expected = Path(relative)
    candidates: list[object] = []
    candidates.append(repo.get("upstream_config"))
    components = repo.get("components", {})
    if isinstance(components, dict):
        for component in components.values():
            if isinstance(component, dict):
                candidates.append(component.get("upstream_config"))

    return any(
        Path(str(candidate)) == expected for candidate in candidates if candidate
    )


def remove_cleanup_findings(findings: list[CleanupFinding]) -> None:
    for finding in findings:
        if finding.path.is_dir():
            shutil.rmtree(finding.path)
        else:
            finding.path.unlink()


def _retired_path_has_tracked_content(repo_path: Path, relative: str) -> bool:
    try:
        result = subprocess.run(  # nosec B603 B607
            ["git", "ls-files", "--", relative],
            cwd=repo_path,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())
