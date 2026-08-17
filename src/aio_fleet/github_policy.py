from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

import yaml


def load_policy(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    if "repositories" not in data or not isinstance(data["repositories"], dict):
        raise ValueError(f"{path} must define repositories")
    return data


def validate_github_policy(
    policy_path: Path,
    *,
    repos: list[str] | None = None,
    check_secrets: bool,
) -> list[str]:
    policy = load_policy(policy_path)
    owner = str(policy.get("owner", "wgross19"))
    defaults = policy.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("github policy defaults must be a mapping")

    configured = policy["repositories"]
    selected = repos or sorted(configured)
    failures: list[str] = []
    for repo_name in selected:
        if repo_name not in configured:
            failures.append(f"{repo_name}: missing from github policy")
            continue
        repo_policy = _deep_merge(defaults, configured[repo_name])
        failures.extend(_repository_failures(owner, repo_name, repo_policy))
        failures.extend(_branch_protection_failures(owner, repo_name, repo_policy))
        failures.extend(_action_permission_failures(owner, repo_name, repo_policy))
        if check_secrets:
            failures.extend(_secret_failures(owner, repo_name, repo_policy))
    return failures


def _repository_failures(
    owner: str, repo_name: str, policy: dict[str, Any]
) -> list[str]:
    expected = dict(policy.get("repository", {}))
    if not expected:
        return []
    data = _gh_json(["api", f"repos/{owner}/{repo_name}"])
    failures: list[str] = []
    comparisons = {
        "visibility": data.get("visibility"),
        "homepage_url": data.get("homepage"),
        "has_issues": data.get("has_issues"),
        "has_projects": data.get("has_projects"),
        "has_wiki": data.get("has_wiki"),
        "delete_branch_on_merge": data.get("delete_branch_on_merge"),
        "allow_auto_merge": data.get("allow_auto_merge"),
        "allow_merge_commit": data.get("allow_merge_commit"),
        "allow_rebase_merge": data.get("allow_rebase_merge"),
        "allow_squash_merge": data.get("allow_squash_merge"),
        "allow_update_branch": data.get("allow_update_branch"),
    }
    for key, actual in comparisons.items():
        if key in expected and actual != expected[key]:
            failures.append(
                f"{repo_name}: repository {key} expected {expected[key]!r}, got {actual!r}"
            )
    return failures


def _branch_protection_failures(
    owner: str, repo_name: str, policy: dict[str, Any]
) -> list[str]:
    branch_protection = policy.get("branch_protection", {})
    if not isinstance(branch_protection, dict):
        return []
    expected = dict(branch_protection)
    if not expected:
        return []
    branch = str(policy.get("branch", policy.get("default_branch", "main")))
    data = _gh_json(["api", f"repos/{owner}/{repo_name}/branches/{branch}/protection"])

    expected_checks = list(policy.get("required_checks", []))
    actual_checks = data.get("required_status_checks", {}).get("contexts", [])
    failures: list[str] = []
    if set(actual_checks) != set(expected_checks):
        failures.append(
            f"{repo_name}: required checks drift: expected {sorted(expected_checks)}, got {sorted(actual_checks)}"
        )
    expected_check_app_id = policy.get("required_check_app_id")
    expected_check_app_ids = _expected_check_app_ids(policy, expected_checks)
    unknown_expected_check_app_ids = sorted(
        set(expected_check_app_ids) - set(expected_checks)
    )
    for context in unknown_expected_check_app_ids:
        failures.append(
            f"{repo_name}: required_check_app_ids declares unknown required check {context!r}"
        )
    known_expected_check_app_ids = {
        context: app_id
        for context, app_id in expected_check_app_ids.items()
        if context in expected_checks
    }
    per_check_app_ids = policy.get("required_check_app_ids")
    enforce_check_app_coverage = expected_check_app_id is not None or bool(per_check_app_ids)
    if enforce_check_app_coverage:
        missing_expected_check_app_ids = sorted(
            set(expected_checks) - set(known_expected_check_app_ids)
        )
        for context in missing_expected_check_app_ids:
            failures.append(
                f"{repo_name}: required check {context!r} is missing required_check_app_id coverage"
            )

    if known_expected_check_app_ids:
        check_app_failures = _required_check_app_failures(
            repo_name,
            data.get("required_status_checks", {}).get("checks", []),
            known_expected_check_app_ids,
        )
        failures.extend(check_app_failures)
    elif expected_check_app_id is not None:
        failures.append(f"{repo_name}: required_check_app_id must be numeric")

    strict = data.get("required_status_checks", {}).get("strict")
    if (
        "strict_required_status_checks" in expected
        and strict != expected["strict_required_status_checks"]
    ):
        failures.append(
            f"{repo_name}: required status strict expected {expected['strict_required_status_checks']}, got {strict}"
        )

    checks = {
        "enforce_admins": data.get("enforce_admins", {}).get("enabled"),
        "require_conversation_resolution": data.get(
            "required_conversation_resolution", {}
        ).get("enabled"),
        "require_signed_commits": data.get("required_signatures", {}).get("enabled"),
        "required_approving_review_count": data.get(
            "required_pull_request_reviews", {}
        ).get("required_approving_review_count"),
    }
    for key, actual in checks.items():
        if key in expected and actual != expected[key]:
            failures.append(
                f"{repo_name}: branch protection {key} expected {expected[key]!r}, got {actual!r}"
            )
    return failures


def _required_check_app_failures(
    repo_name: str,
    checks: Any,
    expected_app_ids: dict[str, int],
) -> list[str]:
    if not isinstance(checks, list):
        checks = []
    by_context = {
        str(check.get("context", "")): check.get("app_id")
        for check in checks
        if isinstance(check, dict)
    }
    failures: list[str] = []
    for context, expected_app_id in expected_app_ids.items():
        actual = by_context.get(context)
        if actual != expected_app_id:
            failures.append(
                f"{repo_name}: required check {context!r} app_id expected {expected_app_id}, got {actual!r}"
            )
    return failures


def _expected_check_app_ids(
    policy: dict[str, Any], expected_checks: list[str]
) -> dict[str, int]:
    expected: dict[str, int] = {}
    fallback = policy.get("required_check_app_id")
    if fallback is not None:
        try:
            fallback = int(fallback)
        except (TypeError, ValueError):
            return expected
        expected.update({context: fallback for context in expected_checks})

    per_check = policy.get("required_check_app_ids", {})
    if per_check is None:
        return expected
    if not isinstance(per_check, dict):
        return expected
    for context, app_id in per_check.items():
        try:
            expected[str(context)] = int(app_id)
        except (TypeError, ValueError):
            continue
    return expected


def _action_permission_failures(
    owner: str, repo_name: str, policy: dict[str, Any]
) -> list[str]:
    expected = dict(policy.get("actions", {}))
    if not expected:
        return []
    permissions = _gh_json(["api", f"repos/{owner}/{repo_name}/actions/permissions"])
    failures: list[str] = []

    for key in ["enabled", "allowed_actions", "sha_pinning_required"]:
        if key in expected and permissions.get(key) != expected[key]:
            failures.append(
                f"{repo_name}: actions {key} expected {expected[key]!r}, got {permissions.get(key)!r}"
            )

    selected_keys = {"github_owned_allowed", "verified_allowed", "patterns_allowed"}
    if not selected_keys.intersection(expected):
        return failures

    if permissions.get("allowed_actions") != "selected":
        failures.append(
            f"{repo_name}: selected actions cannot be validated while actions allowed_actions is "
            f"{permissions.get('allowed_actions')!r}"
        )
        return failures

    try:
        selected = _gh_json(
            [
                "api",
                f"repos/{owner}/{repo_name}/actions/permissions/selected-actions",
            ]
        )
    except RuntimeError as exc:
        failures.append(f"{repo_name}: selected actions unavailable: {exc}")
        return failures

    for key in ["github_owned_allowed", "verified_allowed"]:
        if key in expected and selected.get(key) != expected[key]:
            failures.append(
                f"{repo_name}: selected actions {key} expected {expected[key]!r}, got {selected.get(key)!r}"
            )

    expected_patterns = set(str(item) for item in expected.get("patterns_allowed", []))
    actual_patterns = set(str(item) for item in selected.get("patterns_allowed", []))
    if expected_patterns != actual_patterns:
        failures.append(
            f"{repo_name}: selected action patterns drift: expected {sorted(expected_patterns)}, "
            f"got {sorted(actual_patterns)}"
        )
    return failures


def _secret_failures(owner: str, repo_name: str, policy: dict[str, Any]) -> list[str]:
    required = {str(item) for item in policy.get("required_secrets", [])}
    if not required:
        return []
    data = _gh_json(
        ["secret", "list", "--repo", f"{owner}/{repo_name}", "--json", "name"]
    )
    present = {str(item["name"]) for item in data}
    missing = sorted(required - present)
    return [
        f"{repo_name}: missing required repository secret {name}" for name in missing
    ]


def _gh_json(args: list[str]) -> Any:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI is required for GitHub policy validation")
    env = _gh_env()
    result = subprocess.run(  # nosec B603
        [gh, *args], check=False, text=True, capture_output=True, env=env
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh {' '.join(args)} failed")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("GH_TOKEN"):
        for key in ("AIO_FLEET_WORKFLOW_TOKEN", "AIO_FLEET_CHECK_TOKEN", "APP_TOKEN"):
            if env.get(key):
                env["GH_TOKEN"] = env[key]
                break
    return env


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
