from __future__ import annotations

from pathlib import Path
from typing import Any

from aio_fleet import github_policy


def test_github_policy_declares_superagent_for_all_fleet_repos() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = github_policy.load_policy(root / "infra/github/github-policy.yml")
    repos = manifest["repositories"]

    expected_repos = {
        "aio-fleet",
        "awesome-unraid",
        "gbrain-aio",
        "honcho-aio",
        "fastcrw-aio",
    }
    assert set(repos) == expected_repos  # nosec B101
    for name in expected_repos:
        checks = set(repos[name]["required_checks"])
        app_ids = repos[name].get("required_check_app_ids", {})
        assert {"Superagent Security Scan", "Contributor trust"} <= checks  # nosec B101
        assert app_ids["Superagent Security Scan"] == 3287076  # nosec B101
        assert app_ids["Contributor trust"] == 3287076  # nosec B101


def test_validate_github_policy_detects_required_check_and_action_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
    homepage_url: https://aethereal.dev
    has_issues: true
    has_projects: false
    has_wiki: false
    delete_branch_on_merge: true
    allow_auto_merge: false
    allow_rebase_merge: false
  branch_protection:
    strict_required_status_checks: true
    enforce_admins: false
    require_conversation_resolution: true
    require_signed_commits: true
    required_approving_review_count: 0
  actions:
    enabled: true
    allowed_actions: selected
    github_owned_allowed: true
    verified_allowed: true
    sha_pinning_required: true
    patterns_allowed:
      - wgross19/aio-fleet/.github/workflows/aio-*.yml@*
repositories:
  example-aio:
    required_checks:
      - aio-build / validate-template
    required_check_app_id: 12345
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {
                "visibility": "public",
                "homepage": "https://aethereal.dev",
                "has_issues": True,
                "has_projects": False,
                "has_wiki": False,
                "delete_branch_on_merge": True,
                "allow_auto_merge": False,
                "allow_rebase_merge": True,
            }
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": [],
                    "checks": [],
                    "strict": True,
                },
                "enforce_admins": {"enabled": False},
                "required_conversation_resolution": {"enabled": True},
                "required_signatures": {"enabled": True},
                "required_pull_request_reviews": {"required_approving_review_count": 0},
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {
                "enabled": True,
                "allowed_actions": "selected",
                "sha_pinning_required": True,
            }
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {
                "github_owned_allowed": True,
                "verified_allowed": True,
                "patterns_allowed": [],
            }
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert any("required checks drift" in failure for failure in failures)  # nosec B101
    assert any(
        "allow_rebase_merge expected False" in failure for failure in failures
    )  # nosec B101
    assert any(
        "required check 'aio-build / validate-template' app_id expected 12345"
        in failure
        for failure in failures
    )  # nosec B101
    assert any(
        "selected action patterns drift" in failure for failure in failures
    )  # nosec B101


def test_validate_github_policy_accepts_required_check_app_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
    allow_rebase_merge: false
  branch_protection:
    strict_required_status_checks: true
    require_signed_commits: true
  actions:
    enabled: true
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
    required_check_app_id: 3565017
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {
                "visibility": "public",
                "allow_rebase_merge": False,
            }
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["aio-fleet / required"],
                    "checks": [{"context": "aio-fleet / required", "app_id": 3565017}],
                    "strict": True,
                },
                "required_signatures": {"enabled": True},
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {"enabled": True}
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    assert (  # nosec B101
        github_policy.validate_github_policy(
            policy, repos=["example-aio"], check_secrets=False
        )
        == []
    )


def test_validate_github_policy_reports_all_actions_without_selected_endpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
    allowed_actions: selected
    github_owned_allowed: true
    verified_allowed: true
    sha_pinning_required: true
    patterns_allowed:
      - wgross19/aio-fleet/.github/workflows/aio-*.yml@*
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
    required_check_app_id: 3565017
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["aio-fleet / required"],
                    "checks": [{"context": "aio-fleet / required", "app_id": 3565017}],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {
                "enabled": True,
                "allowed_actions": "all",
                "sha_pinning_required": True,
            }
        if "selected-actions" in joined:
            raise AssertionError("selected-actions endpoint should not be called")
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert failures == [  # nosec B101
        "example-aio: actions allowed_actions expected 'selected', got 'all'",
        "example-aio: selected actions cannot be validated while actions allowed_actions is 'all'",
    ]


def test_validate_github_policy_reports_selected_actions_api_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
    allowed_actions: selected
    github_owned_allowed: true
    verified_allowed: true
    sha_pinning_required: true
    patterns_allowed:
      - wgross19/aio-fleet/.github/workflows/aio-*.yml@*
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
    required_check_app_id: 3565017
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["aio-fleet / required"],
                    "checks": [{"context": "aio-fleet / required", "app_id": 3565017}],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {
                "enabled": True,
                "allowed_actions": "selected",
                "sha_pinning_required": True,
            }
        if "selected-actions" in joined:
            raise RuntimeError("conflict")
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert failures == [  # nosec B101
        "example-aio: selected actions unavailable: conflict"
    ]


def test_validate_github_policy_accepts_per_check_app_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
      - Superagent Security Scan
      - Contributor trust
    required_check_app_id: 3565017
    required_check_app_ids:
      Superagent Security Scan: 3287076
      Contributor trust: 3287076
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": [
                        "aio-fleet / required",
                        "Superagent Security Scan",
                        "Contributor trust",
                    ],
                    "checks": [
                        {"context": "aio-fleet / required", "app_id": 3565017},
                        {"context": "Superagent Security Scan", "app_id": 3287076},
                        {"context": "Contributor trust", "app_id": 3287076},
                    ],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {"enabled": True}
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    assert (  # nosec B101
        github_policy.validate_github_policy(
            policy, repos=["example-aio"], check_secrets=False
        )
        == []
    )


def test_validate_github_policy_rejects_unknown_per_check_app_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
    required_check_app_id: 3565017
    required_check_app_ids:
      Superagent Security Scan: 3287076
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["aio-fleet / required"],
                    "checks": [{"context": "aio-fleet / required", "app_id": 3565017}],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {"enabled": True}
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert failures == [  # nosec B101
        "example-aio: required_check_app_ids declares unknown required check 'Superagent Security Scan'"
    ]


def test_validate_github_policy_rejects_missing_per_check_app_id_coverage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
repositories:
  example-aio:
    required_checks:
      - Superagent Security Scan
      - Contributor trust
    required_check_app_ids:
      Superagent Security Scan: 3287076
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["Superagent Security Scan", "Contributor trust"],
                    "checks": [
                        {"context": "Superagent Security Scan", "app_id": 3287076},
                        {"context": "Contributor trust", "app_id": 999999},
                    ],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {"enabled": True}
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert failures == [  # nosec B101
        "example-aio: required check 'Contributor trust' is missing required_check_app_id coverage"
    ]


def test_validate_github_policy_rejects_same_context_wrong_app_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "github-policy.yml"
    policy.write_text("""
owner: wgross19
defaults:
  repository:
    visibility: public
  branch_protection:
    strict_required_status_checks: true
  actions:
    enabled: true
repositories:
  example-aio:
    required_checks:
      - aio-fleet / required
    required_check_app_id: 3565017
""")

    def fake_gh_json(args: list[str]) -> Any:
        joined = " ".join(args)
        if joined == "api repos/wgross19/example-aio":
            return {"visibility": "public"}
        if joined == "api repos/wgross19/example-aio/branches/main/protection":
            return {
                "required_status_checks": {
                    "contexts": ["aio-fleet / required"],
                    "checks": [{"context": "aio-fleet / required", "app_id": 999999}],
                    "strict": True,
                }
            }
        if joined == "api repos/wgross19/example-aio/actions/permissions":
            return {"enabled": True}
        if (
            joined
            == "api repos/wgross19/example-aio/actions/permissions/selected-actions"
        ):
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(github_policy, "_gh_json", fake_gh_json)

    failures = github_policy.validate_github_policy(
        policy, repos=["example-aio"], check_secrets=False
    )

    assert failures == [  # nosec B101
        "example-aio: required check 'aio-fleet / required' app_id expected 3565017, got 999999"
    ]


def test_infra_uses_rulesets_for_app_bound_required_checks() -> None:
    main_tf = (Path(__file__).resolve().parents[1] / "infra/github/main.tf").read_text()

    assert (  # nosec B101
        'resource "github_repository_ruleset" "trusted_required_checks"' in main_tf
    )
    assert "required_check_app_ids" in main_tf  # nosec B101
    assert "integration_id = lookup(" in main_tf  # nosec B101
    assert 'dynamic "required_status_checks"' in main_tf  # nosec B101
    assert "length(each.value.required_check_app_ids) == 0" in main_tf  # nosec B101
