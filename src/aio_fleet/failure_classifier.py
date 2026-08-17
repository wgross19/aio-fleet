from __future__ import annotations

import re
from typing import Any

from aio_fleet.public_text import assert_public_text, public_text_safe_value

FAILURE_CLASSES = (
    "ghcr-access",
    "dockerhub-auth",
    "registry-tags-missing",
    "release-history-incomplete",
    "required-check-missing",
    "formatting",
    "image-build",
    "upstream-pin",
    "release-publish-token",
    "signed-commit",
    "integration-test",
    "catalog-drift",
    "github-app-permission",
    "upstream-blocked",
    "workflow-timeout",
    "unknown",
)

_RULES: tuple[dict[str, Any], ...] = (
    {
        "class": "ghcr-access",
        "patterns": (
            r"permission_denied:\s*write_package",
            r"write_package",
            r"installation not allowed to create organization package",
            r"denied:\s*.*ghcr",
        ),
        "confidence": 0.92,
        "next_action": (
            "open the GHCR package settings and grant wgross19/aio-fleet "
            "Actions access with Write permission, then retry the protected publish"
        ),
    },
    {
        "class": "dockerhub-auth",
        "patterns": (
            r"docker hub publish credentials are missing",
            r"unauthorized:\s*incorrect username or password",
            r"denied:\s*requested access to the resource is denied",
            r"docker login .*failed",
        ),
        "confidence": 0.9,
        "next_action": (
            "verify DOCKERHUB_USERNAME and DOCKERHUB_PUBLISH_TOKEN, then rerun "
            "the protected registry publish"
        ),
    },
    {
        "class": "registry-tags-missing",
        "patterns": (
            r"missing or unreachable registry tags",
            r"tag .* not found",
            r"tag is needed when pushing to registry",
            r"manifest unknown",
            r"registry verify.*failed",
        ),
        "confidence": 0.86,
        "next_action": (
            "run the release transaction preflight for the repo/component and "
            "queue a protected registry publish for the expected SHA"
        ),
    },
    {
        "class": "release-history-incomplete",
        "patterns": (
            r"release history incomplete",
            r"fetch tags before trusting release due",
            r"missing locally",
            r"tagless checkout",
            r"shallow checkout",
        ),
        "confidence": 0.82,
        "next_action": (
            "refresh a full-tag checkout and rerun the registry-backed release plan"
        ),
    },
    {
        "class": "required-check-missing",
        "patterns": (
            r"required-check-missing",
            r"aio-fleet / required",
            r"required check",
            r"app-check-permission",
        ),
        "confidence": 0.84,
        "next_action": (
            "rerun the central required check and verify the GitHub App can post "
            "the aio-fleet / required check"
        ),
    },
    {
        "class": "formatting",
        "patterns": (
            r"incorrect formatting",
            r"trunk\s+fmt",
            r"autoformat by running",
            r"\bprettier\b",
            r"trunk=failed",
        ),
        "confidence": 0.9,
        "next_action": (
            "run `trunk fmt` on the changed files (the central overlay flags "
            "unformatted app changes) and push the formatted result"
        ),
    },
    {
        "class": "image-build",
        "patterns": (
            r"returned non-zero exit status",
            r"docker build.*(failed|non-zero)",
            r"failed to solve",
            r"patch (failed|does not apply)",
            r"=>\s*error",
            r"executor failed running",
        ),
        "confidence": 0.85,
        "next_action": (
            "reproduce the image build locally (OrbStack/Docker), fix the "
            "Dockerfile/patch/dependency break, and rerun the central check"
        ),
    },
    {
        "class": "upstream-pin",
        "patterns": (
            r"unexpected upstream gem versions",
            r"unable to find arg upstream",
            r"test_dockerfiles_pin_upstream",
            r"assert .*upstream_version",
            r"pin .*(mismatch|stale)",
        ),
        "confidence": 0.85,
        "next_action": (
            "the upstream bump changed a pinned version/commit; update the "
            "Dockerfile pins (and the pin-policy test) to the reviewed upstream "
            "values, then rerun the central check"
        ),
    },
    {
        "class": "release-publish-token",
        "patterns": (
            r"set the gh_token",
            r"github release publish failed",
            r"credential-gap.*gh_token",
            r"gh: to use github cli",
        ),
        "confidence": 0.88,
        "next_action": (
            "ensure the protected publish step exports a gh token "
            "(AIO_FLEET_RELEASE_TOKEN -> GH_TOKEN) for the release publish CLI, "
            "then rerun the protected publish"
        ),
    },
    {
        "class": "signed-commit",
        "patterns": (
            r"required.*signature",
            r"commits must have verified signatures",
            r"base branch policy prohibits",
            r"unsigned commit",
        ),
        "confidence": 0.83,
        "next_action": (
            "recreate the branch commit as a GitHub-signed (verified) commit "
            "(create-pull-request sign-commits, or the createCommitOnBranch API)"
        ),
    },
    {
        "class": "integration-test",
        "patterns": (
            r"integration-tests?",
            r"tests/integration",
            r"pytest.*failed",
            r"failed.*pytest",
        ),
        "confidence": 0.78,
        "next_action": (
            "open the failed control-check log, fix the app runtime/test failure, "
            "and rerun the central check before publishing"
        ),
    },
    {
        "class": "catalog-drift",
        "patterns": (
            r"catalog drift",
            r"catalog-sync-needed",
            r"catalog target",
            r"validate-catalog",
            r"awesome-unraid",
        ),
        "confidence": 0.8,
        "next_action": (
            "run catalog sync in dry-run mode, review the XML/icon diff, and open "
            "the catalog PR if source metadata changed"
        ),
    },
    {
        "class": "github-app-permission",
        "patterns": (
            r"resource not accessible by integration",
            r"bad credentials",
            r"github app",
            r"403 forbidden",
            r"must have admin rights to repository",
        ),
        "confidence": 0.82,
        "next_action": (
            "verify the GitHub App installation, repository access, and requested "
            "workflow permissions before retrying"
        ),
    },
    {
        "class": "upstream-blocked",
        "patterns": (
            r"upstream.*blocked",
            r"missing configured submodule ref",
            r"blocked_reason",
            r"upstream monitor failed",
        ),
        "confidence": 0.86,
        "next_action": (
            "resolve the upstream blocker in the source repo, then rerun upstream "
            "monitor before queueing release work"
        ),
    },
    {
        "class": "workflow-timeout",
        "patterns": (
            r"timed out",
            r"timeout",
            r"cancelled",
            r"stale pending",
            r"operation was canceled",
        ),
        "confidence": 0.72,
        "next_action": (
            "rerun the failed workflow after checking whether the previous run was "
            "cancelled or exceeded its timeout"
        ),
    },
)


def classify_failure_text(
    text: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify sanitized workflow/control-plane failure text."""

    metadata = metadata or {}
    haystack = text.lower()
    matched = next(
        (
            rule
            for rule in _RULES
            if any(
                re.search(pattern, haystack, re.IGNORECASE)
                for pattern in rule["patterns"]
            )
        ),
        None,
    )
    if matched is None:
        root_cause = "unknown"
        confidence = 0.2
        next_action = (
            "inspect the failed job log, classify the root cause, and add a new "
            "classifier rule if this is a repeatable failure mode"
        )
    else:
        root_cause = str(matched["class"])
        confidence = float(matched["confidence"])
        next_action = str(matched["next_action"])
    summary = _summary_for(text, root_cause=root_cause)
    record = {
        "id": _classification_id(metadata, root_cause),
        "root_cause": root_cause,
        "confidence": confidence,
        "summary": summary,
        "next_action": next_action,
        "repo": str(metadata.get("repo", "")),
        "component": str(metadata.get("component", "")),
        "sha": str(metadata.get("sha", "")),
        "run_id": str(metadata.get("run_id", "")),
        "run_url": str(metadata.get("run_url", "")),
        "job": str(metadata.get("job", "")),
        "step": str(metadata.get("step", "")),
    }
    safe = public_text_safe_value(record)
    assert_public_text(str(safe), context="failure classification")
    return safe


def classify_failure_record(
    record: dict[str, Any],
    *,
    log_text: str = "",
) -> dict[str, Any]:
    """Classify a workflow run/job record plus optional failed log text."""

    metadata = {
        "repo": record.get("repo", ""),
        "component": record.get("component", ""),
        "sha": record.get("sha", ""),
        "run_id": record.get("id") or record.get("run_id", ""),
        "run_url": record.get("url", ""),
        "job": record.get("job", ""),
        "step": record.get("step", ""),
    }
    text = "\n".join(
        part
        for part in (
            str(record.get("title", "")),
            str(record.get("conclusion", "")),
            str(record.get("status", "")),
            str(record.get("detail", "")),
            log_text,
        )
        if part
    )
    return classify_failure_text(text, metadata=metadata)


def classify_workflow_state(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return failure classifications for the workflow health payload."""

    last_failure = workflow.get("last_failure")
    if not isinstance(last_failure, dict) or not last_failure:
        return []
    if _workflow_recovered_after_failure(workflow, last_failure):
        return []
    return [classify_failure_record({**last_failure, "repo": workflow.get("repo", "")})]


def _workflow_recovered_after_failure(
    workflow: dict[str, Any], last_failure: dict[str, Any]
) -> bool:
    latest = workflow.get("latest")
    latest = latest if isinstance(latest, dict) else {}
    last_success = workflow.get("last_success")
    last_success = last_success if isinstance(last_success, dict) else {}
    candidates = [latest, last_success]
    seen: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        if _success_recovered_failure(candidate, last_failure):
            return True
    return False


def _success_recovered_failure(
    success: dict[str, Any], failure: dict[str, Any]
) -> bool:
    if success.get("conclusion") != "success":
        return False
    if not _same_workflow_context(success, failure):
        return False
    success_time = str(success.get("updated_at") or success.get("created_at") or "")
    failure_time = str(failure.get("updated_at") or failure.get("created_at") or "")
    if success_time and failure_time:
        return success_time > failure_time
    return success.get("id") != failure.get("id")


def _same_workflow_context(latest: dict[str, Any], failure: dict[str, Any]) -> bool:
    for key in ("branch", "title"):
        latest_value = str(latest.get(key) or "").strip().lower()
        failure_value = str(failure.get(key) or "").strip().lower()
        if latest_value and failure_value and latest_value != failure_value:
            return False
    return True


def _summary_for(text: str, *, root_cause: str) -> str:
    del text
    return f"{root_cause} failure detected"


def _classification_id(metadata: dict[str, Any], root_cause: str) -> str:
    parts = [
        str(metadata.get("run_id", "") or "run"),
        str(metadata.get("job", "") or "job"),
        str(metadata.get("step", "") or "step"),
        root_cause,
    ]
    return ":".join(_slug(part) for part in parts)


def _slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    return value.strip("-") or "unknown"
