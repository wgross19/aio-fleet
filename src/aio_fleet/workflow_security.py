from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

SECRET_MARKERS = ("TOKEN", "SECRET", "PRIVATE_KEY", "PASSWORD", "WEBHOOK")


def audit_workflows(root: Path) -> dict[str, Any]:
    workflow_dir = root / ".github" / "workflows"
    findings: list[dict[str, str]] = []
    workflows: list[str] = []
    if not workflow_dir.exists():
        return {"workflows": [], "findings": [], "ok": True}
    for path in sorted(workflow_dir.glob("*.yml")) + sorted(
        workflow_dir.glob("*.yaml")
    ):
        workflows.append(str(path.relative_to(root)))
        _audit_workflow(path, root=root, findings=findings)
    return {"workflows": workflows, "findings": findings, "ok": not findings}


def _audit_workflow(path: Path, *, root: Path, findings: list[dict[str, str]]) -> None:
    text = path.read_text()
    try:
        workflow = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        findings.append(_finding(path, root, "invalid-yaml", str(exc)))
        return
    if not isinstance(workflow, dict):
        findings.append(
            _finding(path, root, "invalid-workflow", "workflow must be a mapping")
        )
        return
    top_permissions = workflow.get("permissions")
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        _audit_job(
            path,
            root=root,
            job_name=str(job_name),
            job=job,
            findings=findings,
            top_permissions=top_permissions,
        )


def _audit_job(
    path: Path,
    *,
    root: Path,
    job_name: str,
    job: dict[str, Any],
    findings: list[dict[str, str]],
    top_permissions: Any,
) -> None:
    permissions = job.get("permissions")
    if permissions is None and top_permissions is None:
        findings.append(
            _finding(
                path,
                root,
                "permissions",
                f"{job_name}: job permissions are not explicit",
            )
        )
    for index, step in enumerate(job.get("steps", []) or []):
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or f"step {index + 1}")
        uses = step.get("uses")
        if isinstance(uses, str) and "@" in uses:
            ref = uses.rsplit("@", 1)[1]
            if not _looks_pinned_ref(ref):
                findings.append(
                    _finding(
                        path,
                        root,
                        "unpinned-action",
                        f"{job_name}/{name}: action is not pinned to a full commit SHA",
                    )
                )
        if step.get("uses", "").startswith("actions/checkout@"):
            with_config = step.get("with") if isinstance(step.get("with"), dict) else {}
            if with_config.get("persist-credentials") is not False:
                findings.append(
                    _finding(
                        path,
                        root,
                        "checkout-credentials",
                        f"{job_name}/{name}: checkout should set persist-credentials: false",
                    )
                )
        run = step.get("run")
        if isinstance(run, str):
            _audit_run_block(
                path,
                root=root,
                job_name=job_name,
                name=name,
                run=run,
                findings=findings,
            )
        env = step.get("env")
        if isinstance(env, dict) and "GH_TOKEN" in env and "GITHUB_TOKEN" in env:
            findings.append(
                _finding(
                    path,
                    root,
                    "token-scope",
                    f"{job_name}/{name}: both GH_TOKEN and GITHUB_TOKEN are exported",
                )
            )


def _audit_run_block(
    path: Path,
    *,
    root: Path,
    job_name: str,
    name: str,
    run: str,
    findings: list[dict[str, str]],
) -> None:
    if "set -euo pipefail" not in run and "set +e" not in run:
        findings.append(
            _finding(
                path,
                root,
                "shell-strictness",
                f"{job_name}/{name}: run block lacks set -euo pipefail or explicit set +e",
            )
        )
    if (
        "${{ github.event.issue.body }}" in run
        or "${{ github.event.pull_request.title }}" in run
    ):
        findings.append(
            _finding(
                path,
                root,
                "untrusted-expression",
                f"{job_name}/{name}: untrusted GitHub event data is interpolated into shell",
            )
        )
    if "<<EOF" in run or "<< EOF" in run:
        findings.append(
            _finding(
                path,
                root,
                "predictable-heredoc",
                f"{job_name}/{name}: predictable heredoc delimiter in shell run block",
            )
        )
    for marker in SECRET_MARKERS:
        if f"echo ${marker}" in run or f'echo "${marker}' in run:
            findings.append(
                _finding(
                    path,
                    root,
                    "secret-output",
                    f"{job_name}/{name}: possible secret echo",
                )
            )


def _looks_pinned_ref(ref: str) -> bool:
    return len(ref) == 40 and all(char in "0123456789abcdefABCDEF" for char in ref)


def _finding(path: Path, root: Path, code: str, message: str) -> dict[str, str]:
    return {"path": str(path.relative_to(root)), "code": code, "message": message}
