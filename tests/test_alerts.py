from __future__ import annotations

import json
import urllib.parse
import urllib.request

from aio_fleet import alerts


def test_kuma_push_encodes_status_and_preserves_existing_query(monkeypatch) -> None:
    seen: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"OK"

    def fake_urlopen(url: str, timeout: int):
        seen.append(url)
        assert timeout == 20  # nosec B101
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    payload = alerts.alert_payload(
        event="control-plane",
        status="failure",
        summary="control-plane failed",
    )

    alerts.send_kuma_push("https://kuma.example/api/push/token?ping=12", payload)

    parsed = urllib.parse.urlsplit(seen[0])
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert query["ping"] == "12"  # nosec B101
    assert query["status"] == "down"  # nosec B101
    assert query["msg"] == "control-plane failed"  # nosec B101


def test_webhook_sends_failure_with_dedupe_key(monkeypatch) -> None:
    seen: list[urllib.request.Request] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"OK"

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        seen.append(request)
        assert timeout == 20  # nosec B101
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    payload = alerts.alert_payload(
        event="registry-audit",
        status="failure",
        summary="missing tags",
        dedupe_key="registry-audit:fleet",
    )

    result = alerts.emit_alert(payload, webhook_url="https://hooks.example/fleet")

    assert result["webhook"] == "sent"  # nosec B101
    body = json.loads(seen[0].data.decode())  # type: ignore[union-attr]
    assert body["dedupe_key"] == "registry-audit:fleet"  # nosec B101
    assert body["status"] == "failure"  # nosec B101
    assert seen[0].headers["User-agent"] == "aio-fleet-alerts/1.0"  # nosec B101


def test_discord_webhook_uses_discord_payload(monkeypatch) -> None:
    seen: list[urllib.request.Request] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b"OK"

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        seen.append(request)
        assert timeout == 20  # nosec B101
        return Response()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    payload = alerts.alert_payload(
        event="upstream-monitor",
        status="warning",
        summary="upstream updates available",
        dedupe_key="upstream-monitor:fleet:all",
        annotations=["sure-aio:aio 0.7.0 -> 0.7.1"],
    )

    alerts.send_webhook("https://discord.com/api/webhooks/example/token", payload)

    body = json.loads(seen[0].data.decode())  # type: ignore[union-attr]
    assert body["allowed_mentions"]["parse"] == []  # nosec B101
    assert body["content"] == "aio-fleet: upstream-monitor warning"  # nosec B101
    assert seen[0].headers["User-agent"] == "aio-fleet-alerts/1.0"  # nosec B101
    assert body["embeds"][0]["title"] == "upstream updates available"  # nosec B101
    assert body["embeds"][0]["description"] == (  # nosec B101
        "- sure-aio:aio 0.7.0 -> 0.7.1"
    )
    assert body["embeds"][0]["footer"]["text"] == (  # nosec B101
        "dedupe=upstream-monitor:fleet:all"
    )


def test_discord_description_includes_failure_excerpt_annotation() -> None:
    payload = alerts.alert_payload(
        event="control-plane",
        status="failure",
        summary="dashboard failed",
        details_url="https://github.com/wgross19/aio-fleet/actions/runs/1",
        annotations=[
            "failure excerpt fleet-dashboard.err: OSError: [Errno 7] "
            "Argument list too long: 'gh'"
        ],
    )

    body = alerts._discord_body(payload)

    description = body["embeds"][0]["description"]
    assert (
        "https://github.com/wgross19/aio-fleet/actions/runs/1" in description
    )  # nosec B101
    assert "failure excerpt fleet-dashboard.err" in description  # nosec B101
    assert "Argument list too long" in description  # nosec B101


def test_success_webhook_is_skipped_unless_recovery_or_forced() -> None:
    payload = alerts.alert_payload(
        event="control-plane",
        status="success",
        summary="control-plane recovered",
    )
    recovery = alerts.alert_payload(
        event="recovery",
        status="success",
        summary="control-plane recovered",
    )

    assert (
        alerts.emit_alert(payload, webhook_url="https://hooks", dry_run=True)["webhook"]
        == "skipped"
    )  # nosec B101
    assert (
        alerts.emit_alert(recovery, webhook_url="https://hooks", dry_run=True)[
            "webhook"
        ]
        == "would-send"
    )  # nosec B101
    assert (
        alerts.emit_alert(
            payload,
            webhook_url="https://hooks",
            force_webhook=True,
            dry_run=True,
        )["webhook"]
        == "would-send"
    )  # nosec B101


def test_upstream_report_alert_detects_updates_and_actions() -> None:
    payload = alerts.payload_from_report(
        event="upstream-monitor",
        status="auto",
        report={
            "repos": [
                {
                    "repo": "sure-aio",
                    "results": [
                        {
                            "component": "aio",
                            "current_version": "0.7.0",
                            "latest_version": "0.7.1",
                            "updates_available": True,
                        }
                    ],
                    "actions": [
                        {
                            "action": "upserted-pr",
                            "url": "https://github.com/wgross19/sure-aio/pull/80",
                        }
                    ],
                }
            ]
        },
    )

    assert payload.status == "success"  # nosec B101
    assert payload.dedupe_key == "upstream-monitor:fleet:all"  # nosec B101
    assert payload.annotations == [  # nosec B101
        "sure-aio: upserted-pr  https://github.com/wgross19/sure-aio/pull/80"
    ]
    assert payload.details["actions"][0]["action"] == "upserted-pr"  # nosec B101
    assert payload.details["notify_success"] is True  # nosec B101
    assert alerts.should_send_webhook(payload) is True  # nosec B101


def test_upstream_noop_success_remains_quiet() -> None:
    payload = alerts.payload_from_report(
        event="upstream-monitor",
        status="auto",
        report={"repos": [{"repo": "sure-aio", "results": [], "actions": []}]},
    )

    assert payload.status == "success"  # nosec B101
    assert payload.summary == "Upstream monitor found no updates"  # nosec B101
    assert alerts.should_send_webhook(payload) is False  # nosec B101


def test_upstream_report_alert_classifies_missing_submodule_ref_as_warning() -> None:
    payload = alerts.payload_from_report(
        event="upstream-monitor",
        status="auto",
        report={
            "repos": [
                {
                    "repo": "mem0-aio",
                    "results": [
                        {
                            "component": "openmemory",
                            "current_version": "v2.0.1",
                            "latest_version": "v2.0.2",
                            "updates_available": True,
                            "state": "blocked",
                            "blocked": True,
                            "blocked_reason": (
                                "missing configured submodule ref "
                                "codex/openmemory-v2.0.2-aio on remote origin"
                            ),
                            "submodule_ref": "codex/openmemory-v2.0.2-aio",
                            "next_action": (
                                "create and push codex/openmemory-v2.0.2-aio"
                            ),
                        }
                    ],
                    "actions": [
                        {
                            "action": "skipped",
                            "reason": "blocked-upstream-update",
                        }
                    ],
                }
            ]
        },
    )

    assert payload.status == "warning"  # nosec B101
    assert (
        payload.summary == "Upstream monitor blocked for 1 component(s)"
    )  # nosec B101
    assert "mem0-aio:openmemory" in payload.annotations[0]  # nosec B101
    assert payload.details["blocked"][0]["submodule_ref"] == (  # nosec B101
        "codex/openmemory-v2.0.2-aio"
    )


def test_registry_report_alert_detects_missing_tags() -> None:
    payload = alerts.payload_from_report(
        event="registry-audit",
        status="auto",
        report={
            "repos": [
                {
                    "repo": "sure-aio",
                    "component": "aio",
                    "failures": ["wgross19/sure-aio:latest: tag not found"],
                }
            ]
        },
    )

    assert payload.status == "failure"  # nosec B101
    assert "1 missing or failed tag" in payload.summary  # nosec B101
    assert payload.annotations == [  # nosec B101
        "sure-aio:aio: 1 missing/failed tag(s); e.g. "
        "wgross19/sure-aio:latest: tag not found"
    ]


def test_report_alert_payload_redacts_local_paths() -> None:
    payload = alerts.payload_from_report(
        event="upstream-monitor",
        status="auto",
        report={
            "repos": [
                {
                    "repo": "sure-aio",
                    "results": [
                        {
                            "component": "aio",
                            "state": "blocked",
                            "current_version": "0.7.1",
                            "latest_version": "0.7.2",
                            "blocked_reason": (
                                "pin drift in "
                                "/home/runner/work/_temp/checkouts/sure-aio/Dockerfile"
                            ),
                            "next_action": (
                                "inspect "
                                "/home/runner/work/_temp/checkouts/sure-aio/Dockerfile"
                            ),
                        }
                    ],
                }
            ]
        },
    )

    body = json.dumps(payload.as_dict(), sort_keys=True)
    assert "/home/runner" not in body  # nosec B101
    assert "<redacted: Linux home path>" in body  # nosec B101


def test_publish_success_alert_includes_registry_and_prerelease_urls() -> None:
    payload = alerts.payload_from_report(
        event="publish",
        status="auto",
        report={
            "repo": "sure-aio",
            "status": "success",
            "components": [
                {
                    "component": "sure-alpha",
                    "dockerhub": [
                        "wgross19/sure-aio-alpha:latest-alpha",
                        "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1",
                    ],
                    "ghcr": [
                        "ghcr.io/wgross19/sure-aio-alpha:latest-alpha",
                        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1",
                    ],
                    "release_package_tag": "0.7.1-alpha.7-aio.1",
                    "github_release": {
                        "url": (
                            "https://github.com/wgross19/sure-aio/releases/tag/"
                            "sure-alpha%2F0.7.1-alpha.7-aio.1"
                        )
                    },
                }
            ],
        },
    )

    assert payload.status == "success"  # nosec B101
    assert "release history" in payload.summary  # nosec B101
    assert payload.annotations == [  # nosec B101
        "sure-aio:sure-alpha package 0.7.1-alpha.7-aio.1"
    ]
    assert payload.details["notify_success"] is True  # nosec B101
    assert alerts.should_send_webhook(payload) is True  # nosec B101
    fields = payload.details["discord_fields"]
    field_values = {field["name"]: field["value"] for field in fields}
    assert field_values["sure-alpha package tag"] == "0.7.1-alpha.7-aio.1"  # nosec B101
    values = "\n".join(field["value"] for field in fields)
    assert "wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" in values  # nosec B101
    assert (
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" in values
    )  # nosec B101
    assert "sure-alpha%2F0.7.1-alpha.7-aio.1" in values  # nosec B101


def test_publish_alert_treats_rolling_publish_without_release_tag_as_success() -> None:
    payload = alerts.payload_from_report(
        event="publish",
        status="auto",
        report={
            "repo": "dify-aio",
            "status": "success",
            "components": [
                {
                    "component": "aio",
                    "dockerhub": [
                        "wgross19/dify-aio:latest",
                        "wgross19/dify-aio:sha-abc123",
                    ],
                    "ghcr": [
                        "ghcr.io/wgross19/dify-aio:latest",
                        "ghcr.io/wgross19/dify-aio:sha-abc123",
                    ],
                    "upstream_version": "1.14.2",
                    "release_package_tag": "",
                }
            ],
        },
    )

    assert payload.status == "success"  # nosec B101
    assert payload.summary == "Published rolling images for dify-aio:aio"  # nosec B101
    assert payload.annotations == [  # nosec B101
        "dify-aio:aio rolling tags; upstream 1.14.2"
    ]
    fields = payload.details["discord_fields"]
    field_values = {field["name"]: field["value"] for field in fields}
    assert field_values["aio package tag"] == (  # nosec B101
        "not expected for this rolling publish; upstream version is 1.14.2"
    )
    assert alerts.should_send_webhook(payload) is True  # nosec B101


def test_publish_alert_warns_when_success_report_has_no_registry_tags() -> None:
    payload = alerts.payload_from_report(
        event="publish",
        status="auto",
        report={
            "repo": "dify-aio",
            "status": "success",
            "components": [
                {
                    "component": "aio",
                    "dockerhub": [],
                    "ghcr": [],
                    "upstream_version": "1.14.2",
                    "release_package_tag": "",
                }
            ],
        },
    )

    assert payload.status == "warning"  # nosec B101
    assert (
        payload.summary == "Publish completed without registry tags for dify-aio:aio"
    )  # nosec B101
    assert payload.annotations == [  # nosec B101
        "dify-aio:aio no registry tags; upstream 1.14.2"
    ]
    fields = payload.details["discord_fields"]
    field_values = {field["name"]: field["value"] for field in fields}
    assert (  # nosec B101
        field_values["aio package tag"] == "missing; upstream version is 1.14.2"
    )
    assert alerts.should_send_webhook(payload) is True  # nosec B101


def test_publish_failure_alert_keeps_component_context() -> None:
    payload = alerts.payload_from_report(
        event="publish",
        status="auto",
        report={
            "repo": "sure-aio",
            "status": "failure",
            "failures": ["github-prerelease-sure-alpha: exit 1"],
            "failure_classes": ["prerelease-mismatch"],
            "components": [
                {
                    "component": "sure-alpha",
                    "dockerhub": ["wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1"],
                    "ghcr": ["ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1"],
                    "release_package_tag": "0.7.1-alpha.7-aio.1",
                    "github_release": {
                        "url": (
                            "https://github.com/wgross19/sure-aio/releases/tag/"
                            "sure-alpha%2F0.7.1-alpha.7-aio.1"
                        )
                    },
                }
            ],
        },
    )

    assert payload.status == "failure"  # nosec B101
    assert alerts.should_send_webhook(payload) is True  # nosec B101
    fields = payload.details["discord_fields"]
    values = "\n".join(field["value"] for field in fields)
    assert "tag: 0.7.1-alpha.7-aio.1" in values  # nosec B101
    assert "github-prerelease-sure-alpha: exit 1" in values  # nosec B101
    assert "prerelease-mismatch" in values  # nosec B101
    assert (  # nosec B101
        "ghcr.io/wgross19/sure-aio-alpha:0.7.1-alpha.7-aio.1" not in values
    )


def test_poll_check_report_alert_includes_target_context() -> None:
    payload = alerts.payload_from_report(
        event="poll-check",
        status="auto",
        report={
            "repo": "nanoclaw-aio",
            "sha": "b" * 40,
            "source": "pr:27",
            "event": "pull_request",
            "status": "failure",
            "failures": ["trunk: exit 1: Incorrect formatting"],
            "failure_classes": ["checkout-mismatch"],
        },
    )

    assert payload.status == "failure"  # nosec B101
    assert payload.repo == "nanoclaw-aio"  # nosec B101
    assert payload.dedupe_key == "poll-check:nanoclaw-aio:all"  # nosec B101
    fields = {
        field["name"]: field["value"] for field in payload.details["discord_fields"]
    }
    assert fields["Source"] == "pr:27"  # nosec B101
    assert fields["SHA"] == "b" * 12  # nosec B101
    assert fields["Failed step"] == "trunk"  # nosec B101
    assert fields["Failure class"] == "checkout-mismatch"  # nosec B101


def test_release_publish_success_alert_includes_github_release_url() -> None:
    payload = alerts.payload_from_report(
        event="release-publish",
        status="auto",
        report={
            "repo": "sure-aio",
            "status": "success",
            "tag": "sure-alpha/0.7.1-alpha.7-aio.1",
            "target": "a" * 40,
            "url": (
                "https://github.com/wgross19/sure-aio/releases/tag/"
                "sure-alpha%2F0.7.1-alpha.7-aio.1"
            ),
        },
    )

    assert payload.status == "success"  # nosec B101
    assert payload.details["notify_success"] is True  # nosec B101
    assert alerts.should_send_webhook(payload) is True  # nosec B101
    assert "GitHub release published" in payload.summary  # nosec B101
