"""Dependency floor enforcement for pinned fleet app images.

Each ``upstream_monitor`` component may declare a ``floor`` block:

    - component: bun
      ...
      floor:
        from_component: gbrain   # component whose pinned ref supplies the floor
        file: package.json       # file read from the provider's upstream repo
        field: engines.bun       # dotted key in the JSON file; value like ">=1.4.4"

``dependency_floor_records`` reads every configured floor from its provider at
the pinned ref and classifies each dependent pin: ok, violation, or skipped.
The central check fails fast on violations, BEFORE unit and integration tests
run, and :func:`apply_dependency_floors` (in ``aio_fleet.upstream``) folds a
satisfying pin bump into an upstream bump PR so a new provider release never
ships a PR that cannot boot.

Enforcement is fail-open: unknown spec forms, unparseable versions, missing
files or fields, and fetch failures produce ``skipped`` entries, never
failures.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aio_fleet.manifest import RepoConfig

_JSONFetcher = Callable[[str], Any]

_FLOOR_SPEC_RE = re.compile(
    r"^\s*(?:(>=|<=|>|<)\s*)?v?(\d+)\.(\d+)\.(\d+)(?:[.\-][0-9A-Za-z.\-]+)?\s*$"
)


@dataclass(frozen=True)
class FloorSpec:
    """A parsed dependency floor, e.g. ``>=1.4.4``. A bare version means ``>=``."""

    op: str
    numbers: tuple[int, int, int]
    raw: str


def parse_floor(spec: object) -> FloorSpec | None:
    """Parse a floor declaration; ``None`` when the form is not enforceable."""
    if not isinstance(spec, str):
        return None
    match = _FLOOR_SPEC_RE.match(spec)
    if match is None:
        return None
    op, major, minor, patch = match.groups()
    return FloorSpec(
        op=op or ">=",
        numbers=(int(major), int(minor), int(patch)),
        raw=spec.strip(),
    )


def version_numbers(value: object) -> tuple[int, int, int] | None:
    """Numeric ``X.Y.Z`` core of a version string; ``None`` when unusable."""
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", str(value or ""))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def satisfies_numbers(numbers: tuple[int, int, int], spec: FloorSpec) -> bool:
    """Numeric comparison against a parsed floor's operator."""
    return _compare(numbers, spec.numbers, spec.op)


def satisfies(pinned: str, spec: FloorSpec) -> bool | None:
    """Compare a pinned version against a floor; ``None`` when unparseable."""
    numbers = version_numbers(pinned)
    if numbers is None:
        return None
    return satisfies_numbers(numbers, spec)


def _compare(
    pinned: tuple[int, int, int], floor: tuple[int, int, int], op: str
) -> bool:
    if op == ">=":
        return pinned >= floor
    if op == ">":
        return pinned > floor
    if op == "<=":
        return pinned <= floor
    if op == "<":
        return pinned < floor
    raise ValueError(f"unsupported floor operator {op!r}")


def floor_from_json(data: object, field: str) -> str:
    """Dotted-key lookup inside a parsed JSON document; ``""`` when absent."""
    value: object = data
    for part in field.split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    return value if isinstance(value, str) else str(value or "")


def load_floor_json(url: str) -> Any:
    """Default fetcher: GET a JSON URL with a 20s timeout; ``None`` on failure."""
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def _record(
    component: str,
    *,
    status: str,
    detail: str,
    dockerfile: Path,
    version_key: str,
    digest_key: str,
    pinned: str,
    provider: str = "",
    provider_repo: str = "",
    provider_ref: str = "",
    floor_file: str = "",
    floor_field: str = "",
    spec: FloorSpec | None = None,
) -> dict[str, Any]:
    hint = ""
    if spec is not None:
        digest_note = f" and update `{digest_key}`" if digest_key else ""
        hint = (
            f"raise `{version_key}` to satisfy `{spec.raw}`{digest_note} "
            f"(provider `{provider}` at {provider_ref[:12] or 'pinned ref'} "
            f"declares it in `{floor_file}`)"
        )
    return {
        "component": component,
        "status": status,
        "detail": detail,
        "dockerfile": str(dockerfile),
        "version_key": version_key,
        "digest_key": digest_key,
        "pinned": pinned,
        "provider": provider,
        "provider_repo": provider_repo,
        "provider_ref": provider_ref,
        "floor_file": floor_file,
        "floor_field": floor_field,
        "spec": spec.raw if spec is not None else "",
        "hint": hint,
    }


def _provider_ref(provider_cfg: dict[str, Any], repo_path: Path) -> str:
    from aio_fleet.upstream import read_arg

    dockerfile_path = repo_path / str(provider_cfg.get("dockerfile", "Dockerfile"))
    for key in ("commit_key", "version_key"):
        arg = str(provider_cfg.get(key, "") or "").strip()
        if not arg:
            continue
        try:
            value = read_arg(dockerfile_path, arg)
        except ValueError:
            continue
        if value:
            return value
    return ""


def dependency_floor_records(
    repo: RepoConfig,
    repo_path: Path,
    *,
    fetch_json: _JSONFetcher | None = None,
    provider_refs: dict[str, str] | None = None,
    pinned_versions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Classify every declared floor for ``repo``.

    ``provider_refs`` and ``pinned_versions`` let a caller simulate pending PR
    pins: component -> the ref used to fetch the provider floor, and
    component -> the version to treat as pinned; both override the Dockerfile
    values.
    """
    from aio_fleet.upstream import monitor_configs, read_arg

    fetch = fetch_json or load_floor_json
    by_component = {str(c.get("component")): c for c in monitor_configs(repo)}
    overrides = provider_refs or {}
    pin_overrides = pinned_versions or {}
    records: list[dict[str, Any]] = []
    for component, config in by_component.items():
        floor_cfg = config.get("floor")
        if not isinstance(floor_cfg, dict):
            continue
        version_key = str(config.get("version_key", "") or "").strip()
        digest_key = str(config.get("digest_key", "") or "").strip()
        dockerfile_path = repo_path / str(config.get("dockerfile", "Dockerfile"))
        provider = str(floor_cfg.get("from_component", "") or "").strip()
        floor_file = str(floor_cfg.get("file", "") or "").strip()
        floor_field = str(floor_cfg.get("field", "") or "").strip()
        provider_cfg = by_component.get(provider)
        if provider_cfg is None:
            records.append(
                _record(
                    component,
                    status="skip",
                    detail=f"floor provider '{provider}' is not configured",
                    dockerfile=dockerfile_path,
                    version_key=version_key,
                    digest_key=digest_key,
                    pinned="",
                )
            )
            continue
        try:
            pinned = read_arg(dockerfile_path, version_key)
        except ValueError:
            pinned = ""
        pinned = str(pin_overrides.get(component) or pinned) if version_key else ""
        provider_repo = str(provider_cfg.get("repo", "") or "").strip()
        spec_ref = str(overrides.get(provider) or "").strip()
        if not spec_ref:
            spec_ref = _provider_ref(provider_cfg, repo_path)
        if not (provider_repo and spec_ref and floor_file and floor_field and version_key):
            records.append(
                _record(
                    component,
                    status="skip",
                    detail="floor configuration is incomplete",
                    dockerfile=dockerfile_path,
                    version_key=version_key,
                    digest_key=digest_key,
                    pinned=pinned,
                    provider=provider,
                    provider_repo=provider_repo,
                    provider_ref=spec_ref,
                    floor_file=floor_file,
                    floor_field=floor_field,
                )
            )
            continue
        url = f"https://raw.githubusercontent.com/{provider_repo}/{spec_ref}/{floor_file}"
        data = fetch(url)
        if data is None:
            records.append(
                _record(
                    component,
                    status="skip",
                    detail=f"floor file unavailable at {url}",
                    dockerfile=dockerfile_path,
                    version_key=version_key,
                    digest_key=digest_key,
                    pinned=pinned,
                    provider=provider,
                    provider_repo=provider_repo,
                    provider_ref=spec_ref,
                    floor_file=floor_file,
                    floor_field=floor_field,
                )
            )
            continue
        raw_spec = floor_from_json(data, floor_field)
        spec = parse_floor(raw_spec)
        if spec is None:
            records.append(
                _record(
                    component,
                    status="skip",
                    detail=f"unparseable floor spec {raw_spec!r}",
                    dockerfile=dockerfile_path,
                    version_key=version_key,
                    digest_key=digest_key,
                    pinned=pinned,
                    provider=provider,
                    provider_repo=provider_repo,
                    provider_ref=spec_ref,
                    floor_file=floor_file,
                    floor_field=floor_field,
                )
            )
            continue
        if version_numbers(pinned) is None:
            records.append(
                _record(
                    component,
                    status="skip",
                    detail=f"unparseable pinned version {pinned!r}",
                    dockerfile=dockerfile_path,
                    version_key=version_key,
                    digest_key=digest_key,
                    pinned=pinned,
                    provider=provider,
                    provider_repo=provider_repo,
                    provider_ref=spec_ref,
                    floor_file=floor_file,
                    floor_field=floor_field,
                )
            )
            continue
        satisfied = satisfies(pinned, spec)
        status = "ok" if satisfied else "violation"
        records.append(
            _record(
                component,
                status=status,
                detail="",
                dockerfile=dockerfile_path,
                version_key=version_key,
                digest_key=digest_key,
                pinned=pinned,
                provider=provider,
                provider_repo=provider_repo,
                provider_ref=spec_ref,
                floor_file=floor_file,
                floor_field=floor_field,
                spec=spec,
            )
        )
    return records


def dependency_floor_violations(
    repo: RepoConfig,
    repo_path: Path,
    *,
    fetch_json: _JSONFetcher | None = None,
    provider_refs: dict[str, str] | None = None,
    pinned_versions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The ``violation`` records from :func:`dependency_floor_records`."""
    records = dependency_floor_records(
        repo,
        repo_path,
        fetch_json=fetch_json,
        provider_refs=provider_refs,
        pinned_versions=pinned_versions,
    )
    return sorted(
        (record for record in records if record["status"] == "violation"),
        key=lambda record: record["component"],
    )