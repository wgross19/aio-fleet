from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from aio_fleet.github_app import read_urlopen_with_retry, resolve_token
from aio_fleet.manifest import RepoConfig

CHECK_NAME = "aio-fleet / required"
API_VERSION = "2022-11-28"

VALID_STATUSES = {"queued", "in_progress", "completed"}
VALID_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "stale",
    "success",
    "timed_out",
}


@dataclass(frozen=True)
class CheckRunResult:
    action: str
    check_run_id: int
    html_url: str


def repo_policy_hash(repo: RepoConfig, *, event: str) -> str:
    payload = {
        "repo": repo.name,
        "event": event,
        "app_slug": repo.app_slug,
        "image_name": repo.image_name,
        "publish_profile": repo.publish_profile,
        "xml_paths": repo.list_value("xml_paths"),
        "catalog_assets": repo.raw.get("catalog_assets", []),
        "test": {
            "unit": repo.get("unit_pytest_args", ""),
            "integration": repo.get("integration_pytest_args", ""),
            "extended": repo.get("extended_integration", None),
        },
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def check_external_id(repo: RepoConfig, *, sha: str, event: str) -> str:
    return f"{repo.name}:{sha}:{repo_policy_hash(repo, event=event)}"


def check_run_payload(
    repo: RepoConfig,
    *,
    sha: str,
    event: str,
    status: str,
    conclusion: str | None = None,
    summary: str = "",
    details_url: str | None = None,
    name: str = CHECK_NAME,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported check status: {status}")
    if status == "completed":
        if conclusion not in VALID_CONCLUSIONS:
            raise ValueError("completed check runs require a valid conclusion")
    elif conclusion is not None:
        raise ValueError("conclusion is only valid for completed check runs")

    payload: dict[str, Any] = {
        "name": name,
        "head_sha": sha,
        "status": status,
        "external_id": check_external_id(repo, sha=sha, event=event),
        "output": {
            "title": name,
            "summary": summary or f"{name} is {status}",
        },
    }
    if conclusion is not None:
        payload["conclusion"] = conclusion
    if details_url:
        payload["details_url"] = details_url
    return payload


def upsert_check_run(
    repo: RepoConfig,
    *,
    sha: str,
    event: str,
    status: str,
    conclusion: str | None = None,
    summary: str = "",
    details_url: str | None = None,
    token: str | None = None,
    name: str = CHECK_NAME,
) -> CheckRunResult:
    token = token or resolve_token(fallback_envs=("AIO_FLEET_CHECK_TOKEN",))
    if not token:
        raise RuntimeError(
            "GitHub App credentials or AIO_FLEET_CHECK_TOKEN are required for check-runs"
        )
    payload = check_run_payload(
        repo,
        sha=sha,
        event=event,
        status=status,
        conclusion=conclusion,
        summary=summary,
        details_url=details_url,
        name=name,
    )
    owner, repo_name = repo.github_repo.split("/", 1)
    existing = _find_existing_check_run(
        owner,
        repo_name,
        sha,
        token,
        external_id=str(payload["external_id"]),
        name=str(payload["name"]),
    )
    if existing:
        check_run_id = int(existing["id"])
        response = _github_request(
            f"https://api.github.com/repos/{owner}/{repo_name}/check-runs/{check_run_id}",
            token=token,
            method="PATCH",
            payload=_update_payload(payload),
        )
        return CheckRunResult(
            action="updated",
            check_run_id=check_run_id,
            html_url=str(response.get("html_url", "")),
        )

    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/check-runs",
        token=token,
        method="POST",
        payload=payload,
    )
    return CheckRunResult(
        action="created",
        check_run_id=int(response["id"]),
        html_url=str(response.get("html_url", "")),
    )


def existing_check_run(
    repo: RepoConfig,
    *,
    sha: str,
    event: str,
    token: str | None = None,
) -> dict[str, Any] | None:
    token = token or resolve_token(fallback_envs=("AIO_FLEET_CHECK_TOKEN",))
    if not token:
        return None
    payload = check_run_payload(repo, sha=sha, event=event, status="queued")
    owner, repo_name = repo.github_repo.split("/", 1)
    return _find_existing_check_run(
        owner,
        repo_name,
        sha,
        token,
        external_id=str(payload["external_id"]),
        name=str(payload["name"]),
    )


def check_run_satisfied(
    repo: RepoConfig,
    *,
    sha: str,
    event: str,
    token: str | None = None,
) -> bool:
    existing = existing_check_run(repo, sha=sha, event=event, token=token)
    return bool(
        existing
        and existing.get("status") == "completed"
        and existing.get("conclusion") == "success"
    )


def _find_existing_check_run(
    owner: str,
    repo_name: str,
    sha: str,
    token: str,
    *,
    external_id: str,
    name: str,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"check_name": name})
    response = _github_request(
        f"https://api.github.com/repos/{owner}/{repo_name}/commits/{sha}/check-runs?{query}",
        token=token,
        method="GET",
    )
    for check_run in response.get("check_runs", []):
        if isinstance(check_run, dict) and check_run.get("external_id") == external_id:
            return check_run
    return None


def _update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "head_sha"}


def _github_request(
    url: str,
    *,
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(  # nosec B310
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    try:
        raw = read_urlopen_with_retry(request, timeout=30).decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError(_forbidden_check_run_message(url)) from None
        raise
    return json.loads(raw or "{}")


def _forbidden_check_run_message(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    repo = "unknown"
    if len(parts) >= 4 and parts[:2] == ["repos"]:
        repo = f"{parts[2]}/{parts[3]}"
    return (
        f"GitHub check-run request forbidden for {repo}; verify the AIO Fleet "
        "GitHub App installation has Checks: write access for that repo"
    )
