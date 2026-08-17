from __future__ import annotations

import base64
import json
import os
import re
import subprocess  # nosec B404
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET

from aio_fleet.checks import CHECK_NAME
from aio_fleet.manifest import RepoConfig

RISK_KEYWORDS = {
    "auth",
    "breaking",
    "config",
    "docker",
    "env",
    "mail",
    "migration",
    "otel",
    "port",
    "postgres",
    "redis",
    "volume",
}
GITHUB_CLI_TOKEN_KEYS = (
    "AIO_FLEET_DASHBOARD_TOKEN",
    "AIO_FLEET_UPSTREAM_TOKEN",
    "AIO_FLEET_ISSUE_TOKEN",
    "AIO_FLEET_WORKFLOW_TOKEN",
    "AIO_FLEET_CHECK_TOKEN",
    "APP_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


@dataclass(frozen=True)
class SafetyAssessment:
    repo: str
    component: str
    safety_level: str
    confidence: float
    signals: tuple[str, ...]
    warnings: tuple[str, ...]
    failures: tuple[str, ...]
    next_action: str
    config_delta: str
    template_impact: str
    runtime_smoke: str
    changed_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "component": self.component,
            "safety_level": self.safety_level,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "warnings": list(self.warnings),
            "failures": list(self.failures),
            "next_action": self.next_action,
            "config_delta": self.config_delta,
            "template_impact": self.template_impact,
            "runtime_smoke": self.runtime_smoke,
            "changed_files": list(self.changed_files),
        }


def assess_expected_update(
    repo: RepoConfig,
    results: list[Any],
    *,
    changed_files: list[str],
    inspect_release_notes: bool = False,
) -> SafetyAssessment:
    update_results = [
        result for result in results if getattr(result, "updates_available", False)
    ]
    result = update_results[0] if update_results else (results[0] if results else None)
    return _assessment(
        repo,
        result=result,
        changed_files=changed_files,
        pr=None,
        base_ref="main",
        head_ref="",
        signed_state="unknown",
        check_state="missing",
        include_pr_checks=False,
        inspect_release_notes=inspect_release_notes,
    )


def assess_upstream_pr(
    repo: RepoConfig,
    *,
    result: Any | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
    pr: dict[str, Any] | None = None,
    signed_state: str | None = None,
    check_state: str | None = None,
    inspect_release_notes: bool = True,
) -> SafetyAssessment:
    pr_data = dict(pr) if pr else None
    if pr_number and pr_data is None:
        pr_data = _pr_view(repo, pr_number)
    if branch and pr_data is None:
        pr_data = _pr_for_branch(repo, branch)

    base_ref = str((pr_data or {}).get("baseRefName") or "main")
    head_ref = str((pr_data or {}).get("headRefName") or branch or "")
    files = _pr_changed_files(repo, pr_data=pr_data, branch=branch)
    if result is None:
        result = _matching_monitor_result(repo, head_ref or branch)
    return _assessment(
        repo,
        result=result,
        changed_files=files,
        pr=pr_data,
        base_ref=base_ref,
        head_ref=head_ref,
        signed_state=signed_state,
        check_state=check_state,
        include_pr_checks=True,
        inspect_release_notes=inspect_release_notes,
    )


def render_safety_summary(assessment: SafetyAssessment) -> list[str]:
    lines = [
        f"- Safety: `{assessment.safety_level}` ({assessment.confidence:.2f} confidence)",
        f"- Config delta: {assessment.config_delta}",
        f"- Template impact: {assessment.template_impact}",
        f"- Runtime smoke: {assessment.runtime_smoke}",
        f"- Next action: {assessment.next_action}",
    ]
    for failure in assessment.failures[:5]:
        lines.append(f"- Blocking finding: {failure}")
    for warning in assessment.warnings[:5]:
        lines.append(f"- Review warning: {warning}")
    return lines


def _assessment(
    repo: RepoConfig,
    *,
    result: Any | None,
    changed_files: list[str],
    pr: dict[str, Any] | None,
    base_ref: str,
    head_ref: str,
    signed_state: str | None,
    check_state: str | None,
    include_pr_checks: bool,
    inspect_release_notes: bool,
) -> SafetyAssessment:
    component = str(getattr(result, "component", "aio") if result else "aio")
    strategy = str(getattr(result, "strategy", "pr") if result else "pr")
    updates_available = bool(getattr(result, "updates_available", True))
    signals: list[str] = []
    warnings: list[str] = []
    failures: list[str] = []

    if updates_available and strategy == "notify":
        return SafetyAssessment(
            repo=repo.name,
            component=component,
            safety_level="manual",
            confidence=0.2,
            signals=("notify-only upstream strategy",),
            warnings=(),
            failures=(),
            next_action="manual triage required before source PR",
            config_delta="not-assessed",
            template_impact="manual",
            runtime_smoke=_runtime_smoke(repo, pr, signals, warnings, failures),
            changed_files=tuple(sorted(changed_files)),
        )

    expected = _expected_paths(repo, result)
    unexpected = sorted(set(changed_files) - expected) if changed_files else []
    if unexpected:
        failures.append("unexpected upstream PR file(s): " + ", ".join(unexpected))
    elif changed_files:
        signals.append("changed files match upstream commit allowlist")

    config_delta, template_impact = _template_assessment(
        repo,
        component=component,
        changed_files=changed_files,
        base_ref=base_ref,
        head_ref=head_ref,
        warnings=warnings,
        failures=failures,
    )
    runtime_smoke = _runtime_smoke(repo, pr, signals, warnings, failures)
    if inspect_release_notes:
        _release_note_signals(result, signals=signals, warnings=warnings)
    elif result is not None and getattr(result, "updates_available", False):
        warnings.append("release notes inspection pending on dashboard refresh")

    if include_pr_checks:
        state = check_state or _required_check_state(pr)
        if state != "success":
            failures.append(f"{CHECK_NAME} check is {state}")
        signed = signed_state or _signed_state(repo, pr)
        if signed not in {"verified", "not-needed"}:
            failures.append(f"generated commit is not verified: {signed}")

    level = "ok"
    if failures:
        level = "blocked"
    elif warnings:
        level = "warn"
    confidence = _confidence(level, signals, warnings)
    return SafetyAssessment(
        repo=repo.name,
        component=component,
        safety_level=level,
        confidence=confidence,
        signals=tuple(sorted(dict.fromkeys(signals))),
        warnings=tuple(sorted(dict.fromkeys(warnings))),
        failures=tuple(sorted(dict.fromkeys(failures))),
        next_action=_next_action(level, warnings, failures),
        config_delta=config_delta,
        template_impact=template_impact,
        runtime_smoke=runtime_smoke,
        changed_files=tuple(sorted(changed_files)),
    )


def _expected_paths(repo: RepoConfig, result: Any | None) -> set[str]:
    configured = set(repo.list_value("upstream_commit_paths"))
    if configured:
        return configured
    if result is None:
        return set()
    dockerfile = getattr(result, "dockerfile", None)
    if isinstance(dockerfile, Path):
        try:
            return {str(dockerfile.relative_to(repo.path))}
        except ValueError:
            return {str(dockerfile)}
    return set()


def _template_assessment(
    repo: RepoConfig,
    *,
    component: str,
    changed_files: list[str],
    base_ref: str,
    head_ref: str,
    warnings: list[str],
    failures: list[str],
) -> tuple[str, str]:
    xml_sources = _xml_sources(repo, component)
    changed_xml = sorted(set(changed_files) & set(xml_sources))
    required_targets = _required_targets(repo, component)
    target_failures: list[str] = []
    for xml_path in xml_sources:
        text = _ref_file_text(repo, xml_path, head_ref) if head_ref else None
        if text is None:
            local = repo.path / xml_path
            text = local.read_text(encoding="utf-8") if local.exists() else ""
        targets = _xml_targets(text)
        missing = sorted(required_targets - targets)
        if missing:
            target_failures.append(
                f"{xml_path} missing manifest-required Config Target(s): "
                + ", ".join(missing[:20])
            )
    failures.extend(target_failures)

    if not changed_xml:
        if required_targets:
            return "none", "manifest-targets-present"
        return "none", "no-xml-change"

    delta_parts: list[str] = []
    for xml_path in changed_xml:
        base_text = _ref_file_text(repo, xml_path, base_ref)
        head_text = _ref_file_text(repo, xml_path, head_ref)
        if base_text is None or head_text is None:
            warnings.append(f"{xml_path} Config target diff unavailable")
            continue
        base_targets = _xml_targets(base_text)
        head_targets = _xml_targets(head_text)
        added = sorted(head_targets - base_targets)
        removed = sorted(base_targets - head_targets)
        if added or removed:
            warnings.append(
                f"{xml_path} Config Target delta added={added[:20]} removed={removed[:20]}"
            )
            delta_parts.append(f"{xml_path}: +{len(added)} -{len(removed)}")
    if delta_parts:
        return "; ".join(delta_parts), "review-template-config-delta"
    return "none", "xml-changed-without-config-target-delta"


def _runtime_smoke(
    repo: RepoConfig,
    pr: dict[str, Any] | None,
    signals: list[str],
    warnings: list[str],
    failures: list[str],
) -> str:
    integration_args = str(repo.get("integration_pytest_args", "") or "").strip()
    if not integration_args:
        return "not-configured"
    checks = [
        check
        for check in (pr or {}).get("statusCheckRollup", [])
        if isinstance(check, dict)
    ]
    runtime_checks = [
        check for check in checks if _runtime_check_name(str(check.get("name", "")))
    ]
    if not runtime_checks:
        warnings.append(
            "runtime/integration tests are configured but no PR runtime check was found"
        )
        return "configured-not-seen"
    failures_seen = [
        str(check.get("name", "runtime"))
        for check in runtime_checks
        if str(check.get("conclusion", "")).lower()
        in {"failure", "timed_out", "cancelled"}
    ]
    if failures_seen:
        failures.append("runtime/integration check failed: " + ", ".join(failures_seen))
        return "failed"
    if all(
        str(check.get("status", "")).upper() == "COMPLETED" for check in runtime_checks
    ):
        return "passed"
    warnings.append("runtime/integration check is still pending")
    return "pending"


def _release_note_signals(
    result: Any | None,
    *,
    signals: list[str],
    warnings: list[str],
) -> None:
    if result is None or not getattr(result, "updates_available", False):
        return
    text = release_notes_text(result)
    if not text:
        warnings.append("release notes could not be inspected")
        return
    matched = sorted(
        keyword
        for keyword in RISK_KEYWORDS
        if re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
    )
    if matched:
        warnings.append(
            "release notes mention review keyword(s): " + ", ".join(matched)
        )
    else:
        signals.append("release notes scanned without configured risk keywords")


def release_notes_text(result: Any) -> str:
    url = str(getattr(result, "release_notes_url", "") or "").strip()
    version = str(getattr(result, "latest_version", "") or "").strip()
    github_repo = _github_repo_from_release_url(url)
    if github_repo and version:
        for tag in _tag_candidates(version):
            api_url = f"https://api.github.com/repos/{github_repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
            try:
                data = _http_json(api_url)
            except (OSError, urllib.error.URLError, ValueError):
                continue
            if isinstance(data, dict):
                return "\n".join(
                    str(data.get(key, "")) for key in ["tag_name", "name", "body"]
                )
    if not url:
        return ""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # nosec B310
            return response.read(200_000).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError):
        return ""


def _github_repo_from_release_url(url: str) -> str:
    match = re.search(r"github\.com/([^/]+/[^/]+)/releases", url)
    return match.group(1) if match else ""


def _tag_candidates(version: str) -> list[str]:
    candidates = [version]
    if not version.startswith("v"):
        candidates.append(f"v{version}")
    return list(dict.fromkeys(candidates))


def _http_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "aio-fleet"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def _xml_sources(repo: RepoConfig, component: str) -> list[str]:
    sources = [
        str(asset.get("source", ""))
        for asset in repo.raw.get("catalog_assets", [])
        if isinstance(asset, dict) and str(asset.get("source", "")).endswith(".xml")
    ]
    for monitor in repo.raw.get("upstream_monitor", []) or []:
        if (
            not isinstance(monitor, dict)
            or str(monitor.get("component", "aio")) != component
        ):
            continue
        for path in monitor.get("xml_paths", []) or []:
            if str(path).endswith(".xml"):
                sources.append(str(path))
    return sorted(dict.fromkeys(sources or repo.list_value("xml_paths")))


def _required_targets(repo: RepoConfig, component: str) -> set[str]:
    targets: set[str] = set()
    validation = repo.raw.get("validation", {})
    if isinstance(validation, dict):
        targets.update(
            str(item) for item in validation.get("required_targets", []) or []
        )
    for monitor in repo.raw.get("upstream_monitor", []) or []:
        if (
            not isinstance(monitor, dict)
            or str(monitor.get("component", "aio")) != component
        ):
            continue
        monitor_validation = monitor.get("validation", {})
        if isinstance(monitor_validation, dict):
            targets.update(
                str(item)
                for item in monitor_validation.get("required_targets", []) or []
            )
    return targets


def _xml_targets(text: str) -> set[str]:
    if not text.strip():
        return set()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return set()
    return {
        str(config.attrib.get("Target", "")).strip()
        for config in root.findall(".//Config")
        if str(config.attrib.get("Target", "")).strip()
    }


def _matching_monitor_result(repo: RepoConfig, branch: str | None) -> Any | None:
    try:
        from aio_fleet.upstream import monitor_repo

        results = monitor_repo(repo, write=False)
    except Exception:
        return None
    update_results = [result for result in results if result.updates_available]
    if branch:
        for result in update_results:
            latest = str(getattr(result, "latest_version", ""))
            if latest and latest.replace("/", "-") in branch:
                return result
    return update_results[0] if update_results else (results[0] if results else None)


def _pr_view(repo: RepoConfig, number: int) -> dict[str, Any] | None:
    return _gh_json(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo.github_repo,
            "--json",
            "number,title,url,body,files,headRefName,baseRefName,headRefOid,mergeStateStatus,statusCheckRollup",
        ]
    )


def _pr_for_branch(repo: RepoConfig, branch: str) -> dict[str, Any] | None:
    prs = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo.github_repo,
            "--head",
            branch,
            "--base",
            "main",
            "--json",
            "number,title,url,body,files,headRefName,baseRefName,headRefOid,mergeStateStatus,statusCheckRollup",
        ]
    )
    return prs[0] if isinstance(prs, list) and prs else None


def _pr_changed_files(
    repo: RepoConfig,
    *,
    pr_data: dict[str, Any] | None,
    branch: str | None,
) -> list[str]:
    files = (pr_data or {}).get("files", [])
    if isinstance(files, list) and files:
        return sorted(
            str(file.get("path", ""))
            for file in files
            if isinstance(file, dict) and file.get("path")
        )
    if branch:
        result = subprocess.run(  # nosec
            ["git", "diff", "--name-only", f"main...{branch}"],
            cwd=repo.path,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return sorted(
                line.strip() for line in result.stdout.splitlines() if line.strip()
            )
    return []


def _ref_file_text(repo: RepoConfig, path: str, ref: str) -> str | None:
    if not ref:
        return None
    remote = _github_file_text(repo, path, ref)
    if remote is not None:
        return remote
    result = subprocess.run(  # nosec
        ["git", "show", f"{ref}:{path}"],
        cwd=repo.path,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _github_file_text(repo: RepoConfig, path: str, ref: str) -> str | None:
    encoded_path = urllib.parse.quote(path)
    encoded_ref = urllib.parse.quote(ref, safe="")
    data = _gh_json(
        [
            "api",
            f"repos/{repo.github_repo}/contents/{encoded_path}?ref={encoded_ref}",
        ],
        check=False,
    )
    if not isinstance(data, dict) or not data.get("content"):
        return None
    try:
        return base64.b64decode(str(data["content"])).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _required_check_state(pr: dict[str, Any] | None) -> str:
    for check in (pr or {}).get("statusCheckRollup", []):
        if isinstance(check, dict) and check.get("name") == CHECK_NAME:
            if check.get("status") == "COMPLETED":
                return str(check.get("conclusion", "unknown")).lower()
            return str(check.get("status", "unknown")).lower()
    return "missing"


def _signed_state(repo: RepoConfig, pr: dict[str, Any] | None) -> str:
    number = str((pr or {}).get("number") or "")
    if not number:
        return "missing"
    commits = _gh_json(
        ["api", f"repos/{repo.github_repo}/pulls/{number}/commits", "--paginate"],
        check=False,
    )
    if not isinstance(commits, list) or not commits:
        return "unknown"
    reasons: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        verification = commit.get("commit", {}).get("verification", {})
        if isinstance(verification, dict) and verification.get("verified") is True:
            continue
        reasons.append(str(verification.get("reason") or "unverified"))
    return "verified" if not reasons else ",".join(sorted(set(reasons)))


def _runtime_check_name(name: str) -> bool:
    lowered = name.lower()
    return "integration" in lowered or "runtime" in lowered or "container" in lowered


def _next_action(level: str, warnings: list[str], failures: list[str]) -> str:
    if level == "blocked":
        return failures[0] if failures else "resolve safety failure before merge"
    if level == "warn":
        return warnings[0] if warnings else "human review required"
    if level == "manual":
        return "manual triage required"
    return "human review and merge"


def _confidence(level: str, signals: list[str], warnings: list[str]) -> float:
    if level == "blocked":
        return 0.1
    if level == "manual":
        return 0.2
    if level == "warn":
        return max(0.35, 0.65 - (0.05 * len(warnings)))
    return min(0.95, 0.75 + (0.03 * len(signals)))


def _gh_json(args: list[str], *, check: bool = True) -> Any:
    env = _gh_env()
    result = subprocess.run(  # nosec
        ["gh", *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        if check:
            raise RuntimeError(result.stderr.strip() or "gh command failed")
        return None
    text = result.stdout.strip()
    return json.loads(text) if text else None


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    token = ""
    for key in GITHUB_CLI_TOKEN_KEYS:
        token = env.get(key, "").strip()
        if token:
            break
    if token:
        for key in GITHUB_CLI_TOKEN_KEYS:
            env.pop(key, None)
        env["GH_TOKEN"] = token
    return env
