from __future__ import annotations

import sys
import urllib.error
import urllib.request

from aio_fleet import github_app


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_base64url_bytes_strips_padding() -> None:
    assert github_app._base64url_bytes(b"\xff\xee") == "_-4"  # nosec B101


def test_github_app_main_uses_fallback_token(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AIO_FLEET_BOT_TOKEN", "fallback-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "github_app",
            "--fallback-env",
            "AIO_FLEET_BOT_TOKEN",
        ],
    )

    assert github_app.main() == 0  # nosec B101
    assert capsys.readouterr().out.strip() == "fallback-token"  # nosec B101


def test_github_app_main_prefers_app_credentials(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_create_installation_token(
        issuer: str, installation_id: str, private_key: str
    ) -> str:
        calls.append((issuer, installation_id, private_key))
        return "app-token"

    monkeypatch.setenv("AIO_FLEET_APP_CLIENT_ID", "client-123")
    monkeypatch.setenv("AIO_FLEET_APP_ID", "123")
    monkeypatch.setenv("AIO_FLEET_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("AIO_FLEET_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("AIO_FLEET_BOT_TOKEN", "fallback-token")
    monkeypatch.setattr(
        github_app, "create_installation_token", fake_create_installation_token
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "github_app",
            "--fallback-env",
            "AIO_FLEET_BOT_TOKEN",
        ],
    )

    assert github_app.main() == 0  # nosec B101
    assert capsys.readouterr().out.strip() == "app-token"  # nosec B101
    assert calls == [("client-123", "456", "private-key")]  # nosec B101


def test_github_app_main_falls_back_to_app_id(monkeypatch, capsys) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_create_installation_token(
        issuer: str, installation_id: str, private_key: str
    ) -> str:
        calls.append((issuer, installation_id, private_key))
        return "app-token"

    monkeypatch.delenv("AIO_FLEET_APP_CLIENT_ID", raising=False)
    monkeypatch.setenv("AIO_FLEET_APP_ID", "123")
    monkeypatch.setenv("AIO_FLEET_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("AIO_FLEET_APP_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(
        github_app, "create_installation_token", fake_create_installation_token
    )
    monkeypatch.setattr(sys, "argv", ["github_app"])

    assert github_app.main() == 0  # nosec B101
    assert capsys.readouterr().out.strip() == "app-token"  # nosec B101
    assert calls == [("123", "456", "private-key")]  # nosec B101


def test_create_installation_token_retries_transient_github_errors(
    monkeypatch,
) -> None:
    attempts = 0

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                request.full_url, 504, "Gateway Timeout", {}, None
            )
        return _Response(b'{"token":"value"}')

    monkeypatch.setattr(github_app, "_create_jwt", lambda *args: "jwt")
    monkeypatch.setattr(github_app.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(github_app.time, "sleep", lambda *_args: None)

    token = github_app.create_installation_token("123", "456", "private-key")

    assert token == "value"  # nosec B101 B105
    assert attempts == 2  # nosec B101


def test_create_installation_token_does_not_retry_auth_errors(monkeypatch) -> None:
    attempts = 0

    def fake_urlopen(request: urllib.request.Request, timeout: int):
        nonlocal attempts
        attempts += 1
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(github_app, "_create_jwt", lambda *args: "jwt")
    monkeypatch.setattr(github_app.urllib.request, "urlopen", fake_urlopen)

    try:
        github_app.create_installation_token("123", "456", "private-key")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401  # nosec B101
    else:
        raise AssertionError("expected HTTPError")

    assert attempts == 1  # nosec B101
