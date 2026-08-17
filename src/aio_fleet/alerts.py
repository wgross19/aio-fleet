from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

from aio_fleet.public_text import assert_public_text, public_text_safe_value

SUCCESS_STATES = {"success", "succeeded", "ok", "up", "passed"}
FAILURE_STATES = {"failure", "failed", "error", "down", "timed_out", "cancelled"}
WARNING_STATES = {"warning", "blocked", "missing", "updates", "attention"}
WEBHOOK_EVENT_ALLOWLIST = {
    "publish",
    "recovery",
    "release-publish",
    "registry-audit",
    "release-readiness",
    "upstream-monitor",
    "upstream-update",
}
HTTP_USER_AGENT = "aio-fleet-alerts/1.0"


@dataclass(frozen=True)
class AlertPayload:
    event: str
    status: str
    severity: str
    summary: str
    dedupe_key: str
    repo: str = ""
    component: str = ""
    details_url: str = ""
    annotations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_status(status: str) -> str:
    normalized = status.strip().lower().replace(" ", "_") or "success"
    if normalized in SUCCESS_STATES:
        return "success"
    if normalized in FAILURE_STATES:
        return "failure"
    if normalized in WARNING_STATES:
        return "warning"
    return normalized


def alert_payload(
    *,
    event: str,
    status: str,
    summary: str,
    repo: str = "",
    component: str = "",
    details_url: str = "",
    dedupe_key: str = "",
    annotations: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> AlertPayload:
    normalized = normalize_status(status)
    severity = (
        "critical"
        if normalized == "failure"
        else "warning" if normalized == "warning" else "info"
    )
    key_parts = [event, repo or "fleet", component or "all"]
    key = dedupe_key or ":".join(key_parts)
    safe_annotations = public_text_safe_value(annotations or [])
    safe_details = public_text_safe_value(details or {})
    return AlertPayload(
        event=str(public_text_safe_value(event)),
        status=normalized,
        severity=severity,
        summary=str(public_text_safe_value(summary or f"{event}: {normalized}")),
        dedupe_key=str(public_text_safe_value(key)),
        repo=str(public_text_safe_value(repo)),
        component=str(public_text_safe_value(component)),
        details_url=str(public_text_safe_value(details_url)),
        annotations=(safe_annotations if isinstance(safe_annotations, list) else []),
        details=safe_details if isinstance(safe_details, dict) else {},
    )


def payload_from_report(
    *,
    event: str,
    report: dict[str, Any],
    status: str = "auto",
    summary: str = "",
    details_url: str = "",
    dedupe_key: str = "",
) -> AlertPayload:
    derived_status, derived_summary, annotations, details = summarize_report(
        event, report
    )
    repo = _report_repo(report)
    component = _report_component(report)
    return alert_payload(
        event=event,
        status=derived_status if status == "auto" else status,
        summary=summary or derived_summary,
        repo=repo,
        component=component,
        details_url=details_url,
        dedupe_key=dedupe_key,
        annotations=annotations,
        details=details,
    )


def summarize_report(
    event: str, report: dict[str, Any]
) -> tuple[str, str, list[str], dict[str, Any]]:
    repos = report.get("repos", [])
    if not isinstance(repos, list):
        repos = []
    if event == "upstream-monitor":
        errors = [
            item for item in repos if isinstance(item, dict) and item.get("error")
        ]
        blocked: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        for item in repos:
            if not isinstance(item, dict):
                continue
            for result in item.get("results", []):
                if not isinstance(result, dict):
                    continue
                if result.get("blocked") or result.get("state") == "blocked":
                    blocked.append({"repo": item.get("repo", ""), **result})
                elif result.get("updates_available"):
                    updates.append({"repo": item.get("repo", ""), **result})
            for action in item.get("actions", []):
                if isinstance(action, dict) and action.get("action") not in {
                    "skipped",
                    "",
                }:
                    actions.append({"repo": item.get("repo", ""), **action})
        if errors:
            summary = f"Upstream monitor failed for {len(errors)} repo(s)"
            annotations = [
                f"{item.get('repo', 'unknown')}: {item.get('error', 'unknown error')}"
                for item in errors[:10]
            ]
            return "failure", summary, annotations, {"errors": errors}
        if blocked:
            summary = f"Upstream monitor blocked for {len(blocked)} component(s)"
            annotations = [
                (
                    "{repo}:{component} {current_version} -> {latest_version} "
                    "blocked: {blocked_reason}; next={next_action}"
                ).format(
                    repo=item.get("repo", ""),
                    component=item.get("component", "aio"),
                    current_version=item.get("current_version", ""),
                    latest_version=item.get("latest_version", ""),
                    blocked_reason=item.get("blocked_reason", ""),
                    next_action=item.get("next_action", ""),
                )
                for item in blocked[:10]
            ]
            return (
                "warning",
                summary,
                annotations,
                {"blocked": blocked, "updates": updates, "actions": actions},
            )
        pr_actions = [
            action
            for action in actions
            if action.get("action") in {"upserted-pr", "would-create-pr"}
        ]
        if pr_actions:
            summary = f"Upstream PR opened or updated for {len(pr_actions)} repo(s)"
            annotations = []
            for action in pr_actions[:10]:
                repo_name = action.get("repo", "")
                url = action.get("url", "")
                branch = action.get("branch", "")
                annotations.append(
                    f"{repo_name}: {action.get('action')} {branch} {url}".strip()
                )
            return (
                "success",
                summary,
                annotations,
                {
                    "updates": updates,
                    "actions": actions,
                    "notify_success": True,
                    "discord_fields": _action_fields(pr_actions),
                },
            )
        if updates or actions:
            summary = f"Upstream updates available for {len(updates)} component(s)"
            annotations = [
                "{repo}:{component} {current_version} -> {latest_version}".format(
                    repo=item.get("repo", ""),
                    component=item.get("component", "aio"),
                    current_version=item.get("current_version", ""),
                    latest_version=item.get("latest_version", ""),
                )
                for item in updates[:10]
            ]
            return (
                "warning",
                summary,
                annotations,
                {"updates": updates, "actions": actions},
            )
        return "success", "Upstream monitor found no updates", [], {}

    if event == "registry-audit":
        failures: list[dict[str, str]] = []
        for item in repos:
            if not isinstance(item, dict):
                continue
            repo = str(item.get("repo", "unknown"))
            component = str(item.get("component", "aio"))
            for failure in item.get("failures", []):
                failure_text = str(failure).strip()
                if failure_text:
                    failures.append(
                        {
                            "repo": repo,
                            "component": component,
                            "failure": failure_text,
                        }
                    )
        if failures:
            annotations = _registry_failure_annotations(failures)
            return (
                "failure",
                f"Registry audit found {len(failures)} missing or failed tag(s)",
                annotations,
                {
                    "failures": failures,
                    "discord_fields": _registry_failure_fields(failures),
                },
            )
        return "success", "Registry audit passed", [], {}

    if event in {"publish", "release-publish", "poll-check", "control-check"}:
        failures = [
            str(failure)
            for failure in report.get("failures", [])
            if str(failure).strip()
        ]
        components = [
            item for item in report.get("components", []) if isinstance(item, dict)
        ]
        repo = str(report.get("repo", "fleet"))
        if failures or report.get("status") == "failure":
            if event in {"poll-check", "control-check"}:
                summary = f"aio-fleet required check failed for {repo}"
                details = _control_check_details(report, failures)
                return "failure", summary, failures[:5], details
            summary = f"Publish failed for {repo}"
            details: dict[str, Any] = {"failures": failures, "components": components}
            fields = _publish_failure_fields(components)
            fields.extend(_failure_class_fields(report))
            if failures:
                fields.append({"name": "Next action", "value": failures[0]})
            if fields:
                details["discord_fields"] = fields
            return "failure", summary, failures[:10], details
        if event in {"poll-check", "control-check"}:
            return (
                "success",
                f"aio-fleet required check passed for {repo}",
                [],
                {"notify_success": False},
            )
        if event == "release-publish" and report.get("tag"):
            tag = str(report.get("tag", ""))
            summary = f"GitHub release published for {repo}: {tag}"
            return (
                "success",
                summary,
                [str(report.get("url", ""))],
                {"notify_success": True, "discord_fields": _release_fields(report)},
            )
        if components:
            label = ", ".join(
                f"{repo}:{item.get('component', 'aio')}" for item in components[:3]
            )
            publish_components = [
                item
                for item in components
                if not str(item.get("error", "") or "").strip()
            ]
            tagless_components = [
                item
                for item in publish_components
                if not str(item.get("release_package_tag", "") or "").strip()
                and not _publish_component_has_registry_tags(item)
            ]
            rolling_components = [
                item
                for item in publish_components
                if not str(item.get("release_package_tag", "") or "").strip()
                and _publish_component_has_registry_tags(item)
            ]
            has_release_history = any(
                isinstance(item.get("github_release"), dict)
                and item["github_release"].get("url")
                for item in components
            )
            if tagless_components:
                summary = f"Publish completed without registry tags for {label}"
            elif rolling_components and len(rolling_components) == len(
                publish_components
            ):
                summary = f"Published rolling images for {label}"
            elif has_release_history:
                summary = f"Published images and release history for {label}"
            else:
                summary = f"Published images for {label}"
            annotations = []
            for item in components[:5]:
                annotations.append(_publish_component_annotation(repo, item))
            return (
                "warning" if tagless_components else "success",
                summary,
                annotations,
                {
                    "components": components,
                    "notify_success": True,
                    "discord_fields": _publish_fields(components),
                },
            )
        return "success", f"{event}: success", [], {}

    return "success", f"{event}: success", [], {}


def should_send_webhook(payload: AlertPayload, *, force: bool = False) -> bool:
    if force:
        return True
    if payload.event == "recovery":
        return True
    if payload.status in {"failure", "warning"}:
        return True
    if payload.status == "success" and payload.details.get("notify_success"):
        return True
    return payload.event in WEBHOOK_EVENT_ALLOWLIST and payload.status != "success"


def send_kuma_push(push_url: str, payload: AlertPayload) -> str:
    parsed = urllib.parse.urlsplit(push_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "status": "down" if payload.status == "failure" else "up",
            "msg": payload.summary[:180],
        }
    )
    url = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )
    with urllib.request.urlopen(url, timeout=20) as response:  # nosec B310
        response.read()
    return "sent"


def send_webhook(
    webhook_url: str, payload: AlertPayload, *, webhook_format: str = "json"
) -> None:
    if webhook_format == "text":
        body = _text_body(payload).encode()
        headers = {"Content-Type": "text/plain; charset=utf-8"}
    elif _is_discord_webhook(webhook_url):
        body = json.dumps(_discord_body(payload), sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
    else:
        body = json.dumps(payload.as_dict(), sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={**headers, "User-Agent": HTTP_USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310
        response.read()


def emit_alert(
    payload: AlertPayload,
    *,
    kuma_url: str = "",
    webhook_url: str = "",
    webhook_format: str = "json",
    force_webhook: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    assert_public_text(json.dumps(payload.as_dict(), sort_keys=True), context="alert")
    deliveries: dict[str, Any] = {
        "payload": payload.as_dict(),
        "kuma": "skipped",
        "webhook": "skipped",
    }
    if kuma_url:
        deliveries["kuma"] = (
            "would-send" if dry_run else send_kuma_push(kuma_url, payload)
        )
    if webhook_url and should_send_webhook(payload, force=force_webhook):
        if dry_run:
            deliveries["webhook"] = "would-send"
        else:
            send_webhook(webhook_url, payload, webhook_format=webhook_format)
            deliveries["webhook"] = "sent"
    return deliveries


def _text_body(payload: AlertPayload) -> str:
    lines = [
        payload.summary,
        f"status={payload.status}",
        f"dedupe={payload.dedupe_key}",
    ]
    if payload.details_url:
        lines.append(payload.details_url)
    lines.extend(f"- {annotation}" for annotation in payload.annotations)
    return "\n".join(lines) + "\n"


def _is_discord_webhook(webhook_url: str) -> bool:
    hostname = (urllib.parse.urlsplit(webhook_url).hostname or "").lower()
    return hostname in {"discord.com", "www.discord.com", "discordapp.com"}


def _discord_body(payload: AlertPayload) -> dict[str, Any]:
    description = _discord_description(payload)
    if len(description) > 3900:
        description = description[:3897] + "..."
    color = {
        "failure": 0xD73A49,
        "warning": 0xD29922,
        "success": 0x2DA44E,
    }.get(payload.status, 0x57606A)
    embed: dict[str, Any] = {
        "title": payload.summary[:256],
        "color": color,
        "fields": [
            {"name": "Event", "value": payload.event[:1024], "inline": True},
            {"name": "Status", "value": payload.status[:1024], "inline": True},
        ],
        "footer": {"text": f"dedupe={payload.dedupe_key}"[:2048]},
    }
    if description:
        embed["description"] = description
    if payload.repo:
        embed["fields"].append(
            {"name": "Repo", "value": payload.repo[:1024], "inline": True}
        )
    if payload.component:
        embed["fields"].append(
            {"name": "Component", "value": payload.component[:1024], "inline": True}
        )
    if payload.details_url:
        embed["url"] = payload.details_url
    for discord_field in payload.details.get("discord_fields", []):
        if not isinstance(discord_field, dict):
            continue
        name = str(discord_field.get("name", "")).strip()
        value = str(discord_field.get("value", "")).strip()
        if not name or not value:
            continue
        embed["fields"].append(
            {
                "name": name[:256],
                "value": value[:1024],
                "inline": bool(discord_field.get("inline", False)),
            }
        )
    return {
        "content": f"aio-fleet: {payload.event} {payload.status}"[:2000],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def _discord_description(payload: AlertPayload) -> str:
    lines: list[str] = []
    if payload.details_url:
        lines.append(payload.details_url)
    lines.extend(f"- {annotation}" for annotation in payload.annotations[:5])
    return "\n".join(lines)


def _report_repo(report: dict[str, Any]) -> str:
    repo = report.get("repo")
    return str(repo).strip() if repo else ""


def _report_component(report: dict[str, Any]) -> str:
    components = report.get("components", [])
    if not isinstance(components, list) or len(components) != 1:
        return ""
    component = (
        components[0].get("component") if isinstance(components[0], dict) else ""
    )
    return str(component).strip() if component else ""


def _control_check_details(
    report: dict[str, Any], failures: list[str]
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    source = str(report.get("source", "") or "").strip()
    sha = str(report.get("sha", "") or "").strip()
    event = str(report.get("event", "") or "").strip()
    if source:
        fields.append({"name": "Source", "value": source, "inline": True})
    if event:
        fields.append({"name": "Target event", "value": event, "inline": True})
    if sha:
        fields.append({"name": "SHA", "value": sha[:12], "inline": True})
    if failures:
        step = failures[0].split(":", 1)[0]
        if step:
            fields.append({"name": "Failed step", "value": step, "inline": True})
        fields.extend(_failure_class_fields(report))
        fields.append({"name": "Next action", "value": failures[0]})
    return {"failures": failures, "discord_fields": fields}


def _failure_class_fields(report: dict[str, Any]) -> list[dict[str, Any]]:
    classes = [
        str(item).strip()
        for item in report.get("failure_classes", [])
        if str(item).strip()
    ]
    if not classes:
        return []
    return [
        {
            "name": "Failure class",
            "value": ", ".join(classes),
            "inline": True,
        }
    ]


def _registry_failure_annotations(failures: list[dict[str, str]]) -> list[str]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for failure in failures:
        key = (failure["repo"], failure["component"])
        grouped.setdefault(key, []).append(failure["failure"])
    annotations: list[str] = []
    for (repo, component), group in list(grouped.items())[:5]:
        annotations.append(
            f"{repo}:{component}: {len(group)} missing/failed tag(s); e.g. {group[0]}"
        )
    return annotations


def _registry_failure_fields(failures: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for failure in failures:
        key = (failure["repo"], failure["component"])
        grouped.setdefault(key, []).append(failure["failure"])
    fields: list[dict[str, Any]] = []
    for (repo, component), group in list(grouped.items())[:5]:
        fields.append(
            {
                "name": f"{repo}:{component}",
                "value": f"{len(group)} missing/failed tag(s)\n{group[0]}",
            }
        )
    return fields


def _action_fields(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for action in actions[:5]:
        value = str(action.get("url") or action.get("branch") or action.get("action"))
        fields.append(
            {
                "name": str(action.get("repo", "Repo"))[:256],
                "value": value[:1024],
                "inline": False,
            }
        )
    return fields


def _publish_component_has_registry_tags(item: dict[str, Any]) -> bool:
    return bool(item.get("dockerhub") or item.get("ghcr"))


def _publish_fields(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for item in components[:3]:
        component = str(item.get("component", "aio"))
        package_tag = str(item.get("release_package_tag", "") or "").strip()
        upstream_version = str(item.get("upstream_version", "") or "").strip()
        dockerhub = "\n".join(str(tag) for tag in item.get("dockerhub", [])[:4])
        ghcr = "\n".join(str(tag) for tag in item.get("ghcr", [])[:4])
        error = str(item.get("error", "") or "").strip()
        release = item.get("github_release", {})
        release_url = release.get("url", "") if isinstance(release, dict) else ""
        if error:
            fields.append({"name": f"{component} error", "value": error})
        elif package_tag:
            fields.append({"name": f"{component} package tag", "value": package_tag})
        elif upstream_version:
            if _publish_component_has_registry_tags(item):
                value = (
                    "not expected for this rolling publish; "
                    f"upstream version is {upstream_version}"
                )
            else:
                value = f"missing; upstream version is {upstream_version}"
            fields.append(
                {
                    "name": f"{component} package tag",
                    "value": value,
                }
            )
        elif _publish_component_has_registry_tags(item):
            fields.append(
                {
                    "name": f"{component} package tag",
                    "value": "not expected for this rolling publish",
                }
            )
        if dockerhub:
            fields.append({"name": f"{component} Docker Hub", "value": dockerhub})
        if ghcr:
            fields.append({"name": f"{component} GHCR", "value": ghcr})
        if release_url:
            fields.append({"name": f"{component} GitHub release", "value": release_url})
    return fields


def _publish_component_annotation(repo: str, item: dict[str, Any]) -> str:
    component = str(item.get("component", "aio"))
    package_tag = str(item.get("release_package_tag", "") or "").strip()
    if package_tag:
        return f"{repo}:{component} package {package_tag}"
    error = str(item.get("error", "") or "").strip()
    if error:
        return f"{repo}:{component} error"
    upstream_version = str(item.get("upstream_version", "") or "").strip()
    if _publish_component_has_registry_tags(item):
        if upstream_version:
            return f"{repo}:{component} rolling tags; upstream {upstream_version}"
        return f"{repo}:{component} rolling tags"
    if upstream_version:
        return f"{repo}:{component} no registry tags; upstream {upstream_version}"
    return f"{repo}:{component} no registry tags"


def _publish_failure_fields(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for item in components[:3]:
        component = str(item.get("component", "aio"))
        lines: list[str] = []
        package_tag = str(item.get("release_package_tag", "") or "").strip()
        if package_tag:
            lines.append(f"tag: {package_tag}")
        release = item.get("github_release", {})
        release_url = release.get("url", "") if isinstance(release, dict) else ""
        if release_url:
            lines.append(f"release: {release_url}")
        error = str(item.get("error", "") or "").strip()
        if error:
            lines.append(error)
        if lines:
            fields.append({"name": component, "value": "\n".join(lines)})
    return fields


def _release_fields(report: dict[str, Any]) -> list[dict[str, Any]]:
    fields = [
        {"name": "Tag", "value": str(report.get("tag", "")), "inline": True},
        {"name": "Target", "value": str(report.get("target", "")), "inline": True},
    ]
    if report.get("url"):
        fields.append({"name": "Release", "value": str(report["url"])})
    return fields
