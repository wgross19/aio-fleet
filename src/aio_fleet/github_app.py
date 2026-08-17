from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve an AIO fleet automation token."
    )
    parser.add_argument("--client-id-env", default="AIO_FLEET_APP_CLIENT_ID")
    parser.add_argument("--app-id-env", default="AIO_FLEET_APP_ID")
    parser.add_argument(
        "--installation-id-env", default="AIO_FLEET_APP_INSTALLATION_ID"
    )
    parser.add_argument("--private-key-env", default="AIO_FLEET_APP_PRIVATE_KEY")
    parser.add_argument("--fallback-env", action="append", default=[])
    args = parser.parse_args()

    token = resolve_token(
        client_id_env=args.client_id_env,
        app_id_env=args.app_id_env,
        installation_id_env=args.installation_id_env,
        private_key_env=args.private_key_env,
        fallback_envs=tuple(args.fallback_env),
    )
    if token:
        print(token)
        return 0
    return 1


def resolve_token(
    *,
    client_id_env: str = "AIO_FLEET_APP_CLIENT_ID",
    app_id_env: str = "AIO_FLEET_APP_ID",
    installation_id_env: str = "AIO_FLEET_APP_INSTALLATION_ID",
    private_key_env: str = "AIO_FLEET_APP_PRIVATE_KEY",
    fallback_envs: tuple[str, ...] = (),
) -> str:
    client_id = os.environ.get(client_id_env, "").strip()
    app_id = os.environ.get(app_id_env, "").strip()
    installation_id = os.environ.get(installation_id_env, "").strip()
    private_key = os.environ.get(private_key_env, "").strip()
    issuer = client_id or app_id
    if issuer and installation_id and private_key:
        return create_installation_token(issuer, installation_id, private_key)

    for env_name in fallback_envs:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def create_installation_token(
    issuer: str, installation_id: str, private_key: str
) -> str:
    jwt = _create_jwt(issuer, private_key)
    request = urllib.request.Request(  # nosec B310
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        data=b"{}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    payload = json.loads(read_urlopen_with_retry(request, timeout=30).decode("utf-8"))
    token = str(payload.get("token", ""))
    if not token:
        raise RuntimeError(
            "GitHub App installation token response did not include token"
        )
    return token


def read_urlopen_with_retry(
    request: urllib.request.Request | str,
    *,
    timeout: int,
    attempts: int = 4,
) -> bytes:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(  # nosec B310
                request, timeout=timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if not _should_retry_http(exc, attempt=attempt, attempts=attempts):
                raise
            _sleep_before_retry(attempt)
        except (TimeoutError, urllib.error.URLError):
            if attempt >= attempts:
                raise
            _sleep_before_retry(attempt)

    raise RuntimeError("unreachable retry state")


def _should_retry_http(
    exc: urllib.error.HTTPError, *, attempt: int, attempts: int
) -> bool:
    return attempt < attempts and exc.code in TRANSIENT_HTTP_STATUS_CODES


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(min(2 ** (attempt - 1), 8))


def _create_jwt(issuer: str, private_key: str) -> str:
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("openssl is required to sign GitHub App JWTs")

    now = int(time.time())
    header = _base64url_json({"alg": "RS256", "typ": "JWT"})
    payload = _base64url_json({"iat": now - 60, "exp": now + 540, "iss": issuer})
    signing_input = f"{header}.{payload}".encode()
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "app-private-key.pem"
        key_path.write_text(private_key.replace("\\n", "\n"))
        result = subprocess.run(  # nosec B603
            [openssl, "dgst", "-sha256", "-sign", str(key_path)],
            input=signing_input,
            check=False,
            capture_output=True,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    signature = _base64url_bytes(result.stdout)
    return f"{header}.{payload}.{signature}"


def _base64url_json(payload: dict[str, object]) -> str:
    return _base64url_bytes(json.dumps(payload, separators=(",", ":")).encode())


def _base64url_bytes(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


if __name__ == "__main__":
    raise SystemExit(main())
