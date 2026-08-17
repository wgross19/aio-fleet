from __future__ import annotations

import argparse
import re
import shutil
import subprocess  # nosec B404
import tomllib
from pathlib import Path
from typing import Iterable

GIT_BIN = shutil.which("git")
SEMVER_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
AIO_CHANGELOG_HEADING = re.compile(
    r"^##\s+(?:\[(?P<linked>[^\]]+)\]\([^)]+\)|(?P<plain>[^\s]+))"
)
RELEASE_FORMAT_SUBJECT = re.compile(
    r"^chore\(release\): format .+ changelog(?: \(#\d+\))?$"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared AIO fleet release helpers.")
    parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    parser.add_argument(
        "--release-profile",
        choices=["auto", "aio", "semver"],
        default="auto",
        help="Release versioning model. auto uses semver only for the template repo.",
    )
    parser.add_argument("--component", help="Optional component from components.toml.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upstream_parser = subparsers.add_parser("upstream-version")
    upstream_parser.add_argument("--dockerfile", type=Path)
    upstream_parser.add_argument("--upstream-config", type=Path)

    next_parser = subparsers.add_parser("next-version")
    next_parser.add_argument("--dockerfile", type=Path)
    next_parser.add_argument("--upstream-config", type=Path)

    changes_parser = subparsers.add_parser("has-unreleased-changes")
    changes_parser.add_argument("--dockerfile", type=Path)
    changes_parser.add_argument("--upstream-config", type=Path)

    subparsers.add_parser("latest-aio-tag")
    subparsers.add_parser("latest-release-tag")

    latest_parser = subparsers.add_parser("latest-changelog-version")
    latest_parser.add_argument("--changelog", type=Path)

    notes_parser = subparsers.add_parser("extract-release-notes")
    notes_parser.add_argument("version")
    notes_parser.add_argument("--changelog", type=Path)

    commit_parser = subparsers.add_parser("find-release-commit")
    commit_parser.add_argument("version")
    target_parser = subparsers.add_parser("find-release-target-commit")
    target_parser.add_argument("version")

    args = parser.parse_args(argv)
    repo_path = args.repo_path.resolve()
    profile = _release_profile(repo_path, args.release_profile)
    component = _component_config(repo_path, args.component)

    dockerfile = (
        _path_arg(repo_path, getattr(args, "dockerfile", None))
        or component["dockerfile"]
    )
    upstream_config = (
        _path_arg(repo_path, getattr(args, "upstream_config", None))
        or component["upstream_config"]
    )
    changelog = _path_arg(repo_path, getattr(args, "changelog", None)) or (
        repo_path / "CHANGELOG.md"
    )
    suffix = str(component.get("release_suffix", "aio"))

    if args.command == "upstream-version":
        if profile == "semver":
            raise SystemExit("upstream-version is not available for semver releases")
        print(read_upstream_version(dockerfile, upstream_config))
        return 0
    if args.command == "next-version":
        if profile == "semver":
            print(next_semver_release_version(repo_path))
        else:
            print(
                next_aio_release_version(repo_path, dockerfile, upstream_config, suffix)
            )
        return 0
    if args.command == "has-unreleased-changes":
        if profile == "semver":
            print("true" if has_semver_unreleased_changes(repo_path) else "false")
        else:
            print("true" if has_aio_unreleased_changes(repo_path, suffix) else "false")
        return 0
    if args.command == "latest-aio-tag":
        latest_tag = latest_component_release_tag(repo_path, suffix)
        if latest_tag:
            print(latest_tag)
        return 0
    if args.command == "latest-release-tag":
        if profile == "semver":
            latest_tag = latest_semver_tag(repo_path)
        else:
            latest_tag = latest_aio_release_tag(
                repo_path, dockerfile, upstream_config, suffix
            )
        if latest_tag:
            print(latest_tag)
        return 0
    if args.command == "latest-changelog-version":
        if profile == "semver":
            print(latest_changelog_version(changelog, semver=True))
        else:
            upstream_version = read_upstream_version(dockerfile, upstream_config)
            print(
                latest_component_changelog_version(
                    changelog,
                    upstream_version=upstream_version,
                    suffix=suffix,
                )
            )
        return 0
    if args.command == "extract-release-notes":
        print(
            extract_release_notes(args.version, changelog, semver=profile == "semver")
        )
        return 0
    if args.command == "find-release-commit":
        print(find_release_commit(repo_path, args.version))
        return 0
    if args.command == "find-release-target-commit":
        print(find_release_target_commit(repo_path, args.version))
        return 0

    raise SystemExit(f"unknown command: {args.command}")


def read_upstream_version(
    dockerfile: Path,
    upstream_config: Path | None = None,
    *,
    version_key: str | None = None,
) -> str:
    version_key = version_key or load_upstream_version_key(upstream_config)
    pattern = re.compile(rf"^ARG {re.escape(version_key)}=(.+)$")
    for line in dockerfile.read_text().splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1).split("@", 1)[0]
    raise SystemExit(f"Unable to find ARG {version_key} in {dockerfile}")


def load_upstream_version_key(path: Path | None) -> str:
    if path is None or not path.exists():
        return "UPSTREAM_VERSION"
    data = tomllib.loads(path.read_text())
    upstream = data.get("upstream", {})
    if isinstance(upstream, dict):
        return str(upstream.get("version_key", "UPSTREAM_VERSION"))
    return "UPSTREAM_VERSION"


def latest_component_release_tag(repo_path: Path, suffix: str = "aio") -> str | None:
    completed = git_completed(
        repo_path,
        "describe",
        "--tags",
        "--abbrev=0",
        "--match",
        f"*-{suffix}.*",
        "--exclude",
        "*/*",
        "HEAD",
    )
    if completed.returncode != 0:
        return None
    tag = completed.stdout.strip()
    return tag or None


def _aio_release_match_pattern(
    upstream_version: str, suffix: str, tag_prefix: str = ""
) -> re.Pattern[str]:
    version = re.escape(upstream_version)
    optional_v = "v?" if not upstream_version.startswith("v") else ""
    return re.compile(
        rf"^{re.escape(tag_prefix)}(?P<vprefix>{optional_v}){version}-"
        rf"{re.escape(suffix)}\.(?P<revision>\d+)$"
    )


def latest_aio_release_tag(
    repo_path: Path,
    dockerfile: Path,
    upstream_config: Path | None,
    suffix: str = "aio",
    version_key: str | None = None,
    tag_prefix: str = "",
) -> str | None:
    upstream_version = read_upstream_version(
        dockerfile, upstream_config, version_key=version_key
    )
    pattern = _aio_release_match_pattern(upstream_version, suffix, tag_prefix)
    matches: list[tuple[int, str]] = []
    for tag in git_tags(repo_path):
        match = pattern.match(tag)
        if match:
            matches.append((int(match.group("revision")), tag))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def has_aio_unreleased_changes(repo_path: Path, suffix: str = "aio") -> bool:
    latest_tag = latest_component_release_tag(repo_path, suffix)
    if latest_tag is None:
        return True
    return any(commits_since(repo_path, latest_tag))


def next_aio_release_version(
    repo_path: Path,
    dockerfile: Path,
    upstream_config: Path | None,
    suffix: str = "aio",
    version_key: str | None = None,
    tag_prefix: str = "",
) -> str:
    upstream_version = read_upstream_version(
        dockerfile, upstream_config, version_key=version_key
    )
    pattern = _aio_release_match_pattern(upstream_version, suffix, tag_prefix)
    revisions: list[tuple[int, str]] = []
    for tag in git_tags(repo_path):
        match = pattern.match(tag)
        if match:
            revisions.append((int(match.group("revision")), match.group("vprefix")))
    if not revisions:
        return f"{upstream_version}-{suffix}.1"
    revision, version_prefix = max(revisions, key=lambda item: item[0])
    return f"{version_prefix}{upstream_version}-{suffix}.{revision + 1}"


def latest_semver_tag(repo_path: Path) -> str | None:
    tags = []
    for tag in git_tags(repo_path):
        key = semver_key(tag)
        if key is not None:
            tags.append((key, tag if tag.startswith("v") else f"v{tag}"))
    if not tags:
        return None
    tags.sort(key=lambda item: item[0])
    return tags[-1][1]


def has_semver_unreleased_changes(repo_path: Path) -> bool:
    return any(commits_since(repo_path, latest_semver_tag(repo_path)))


def next_semver_release_version(repo_path: Path) -> str:
    latest = latest_semver_tag(repo_path)
    if latest is None:
        return "v0.1.0"

    major, minor, patch = semver_key(latest)  # type: ignore[misc]
    commit_messages = list(commits_since(repo_path, latest))
    has_breaking = any(
        "BREAKING CHANGE" in message or re.match(r"^[a-z]+(\(.+\))?!:", message)
        for message in commit_messages
    )
    has_feature = any(
        re.match(r"^feat(\(.+\))?:", message) for message in commit_messages
    )
    if has_breaking:
        major += 1
        minor = 0
        patch = 0
    elif has_feature:
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"v{major}.{minor}.{patch}"


def semver_key(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.match(tag)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def changelog_versions(changelog: Path, *, semver: bool = False) -> list[str]:
    pattern = re.compile(r"^##\s+([^\s]+)") if semver else AIO_CHANGELOG_HEADING
    versions: list[str] = []
    for line in changelog.read_text().splitlines():
        match = pattern.match(line.strip())
        version = None
        if match and semver:
            version = match.group(1)
        elif match:
            version = match.group("linked") or match.group("plain")
        if version and version != "Unreleased":
            versions.append(version)
    return versions


def latest_changelog_version(changelog: Path, *, semver: bool = False) -> str:
    versions = changelog_versions(changelog, semver=semver)
    if versions:
        return versions[0]
    raise SystemExit(f"Unable to find a released version heading in {changelog}")


def latest_component_changelog_version(
    changelog: Path, *, upstream_version: str, suffix: str = "aio"
) -> str:
    pattern = _aio_release_match_pattern(upstream_version, suffix)
    for version in changelog_versions(changelog):
        if pattern.match(version):
            return version
    raise SystemExit(
        "Unable to find a released "
        f"{suffix} version for {upstream_version} in {changelog}"
    )


def extract_release_notes(
    version: str, changelog: Path, *, semver: bool = False
) -> str:
    if semver:
        heading = re.compile(rf"^##\s+{re.escape(version)}(?:\s+-\s+.+)?$")
    else:
        heading = re.compile(
            rf"^##\s+(?:\[{re.escape(version)}\]\([^)]+\)|{re.escape(version)})(?:\s+-\s+.+)?$"
        )
    next_heading = re.compile(r"^##\s+")
    lines = changelog.read_text().splitlines()
    start = None
    for index, line in enumerate(lines):
        if heading.match(line.strip()):
            start = index + 1
            break
    if start is None:
        raise SystemExit(f"Unable to find release section for {version} in {changelog}")
    end = len(lines)
    for index in range(start, len(lines)):
        if next_heading.match(lines[index].strip()):
            end = index
            break
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise SystemExit(f"Release section for {version} in {changelog} is empty")
    return notes


def find_release_commit(repo_path: Path, version: str) -> str:
    output = git(repo_path, "log", "--format=%H\t%s", "HEAD")
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, subject = line.split("\t", 1)
        if release_subject_matches(subject, version):
            return sha
    raise SystemExit(
        f"Unable to find a merged release commit for {version} on main. "
        f"Expected a chore(release) subject containing {version}."
    )


def release_subject_matches(subject: str, version: str) -> bool:
    normalized = re.sub(r" \(#\d+\)$", "", subject.strip())
    prefix = "chore(release): "
    if not normalized.startswith(prefix):
        return False
    versions = [
        item.strip()
        for item in re.split(r"\s+and\s+|,\s*", normalized.removeprefix(prefix))
        if item.strip()
    ]
    return version in versions


def find_release_target_commit(repo_path: Path, version: str) -> str:
    release_commit = find_release_commit(repo_path, version)
    head = git(repo_path, "rev-parse", "HEAD").strip()
    if release_commit == head:
        return release_commit
    if not git_is_ancestor(repo_path, release_commit, head):
        raise SystemExit(
            f"Release commit {release_commit} for {version} is not reachable from HEAD."
        )
    first_parent_commits = git(
        repo_path, "rev-list", "--first-parent", "--reverse", "HEAD"
    ).splitlines()
    for candidate in first_parent_commits:
        if git_is_ancestor(repo_path, release_commit, candidate):
            return candidate
    return release_commit


def find_release_publish_target_commit(repo_path: Path, version: str) -> str:
    release_target = find_release_target_commit(repo_path, version)
    head = git(repo_path, "rev-parse", "HEAD").strip()
    if release_target == head:
        return release_target
    if not git_is_ancestor(repo_path, release_target, head):
        return release_target
    subjects = git(repo_path, "log", "--format=%s", f"{release_target}..{head}")
    changed_files = git(repo_path, "diff", "--name-only", f"{release_target}..{head}")
    subject_lines = [
        subject.strip() for subject in subjects.splitlines() if subject.strip()
    ]
    changed_paths = [
        path.strip() for path in changed_files.splitlines() if path.strip()
    ]
    if (
        subject_lines
        and all(RELEASE_FORMAT_SUBJECT.match(subject) for subject in subject_lines)
        and changed_paths == ["CHANGELOG.md"]
    ):
        return head
    return release_target


def git_tags(repo_path: Path) -> list[str]:
    output = git(repo_path, "tag", "--list")
    return [line.strip() for line in output.splitlines() if line.strip()]


def commits_since(repo_path: Path, ref: str | None) -> Iterable[str]:
    args = ["log", "--format=%s"]
    if ref:
        args.append(f"{ref}..HEAD")
    output = git(repo_path, *args)
    return [line.strip() for line in output.splitlines() if line.strip()]


def git(repo_path: Path, *args: str) -> str:
    if GIT_BIN is None:
        raise SystemExit("git is required to run release helpers")
    return subprocess.check_output(  # nosec B603
        [GIT_BIN, *args],
        cwd=repo_path,
        text=True,
    ).strip()


def git_completed(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if GIT_BIN is None:
        raise SystemExit("git is required to run release helpers")
    return subprocess.run(  # nosec B603
        [GIT_BIN, *args], cwd=repo_path, text=True, capture_output=True, check=False
    )


def git_is_ancestor(repo_path: Path, ancestor: str, descendant: str) -> bool:
    return (
        git_completed(
            repo_path, "merge-base", "--is-ancestor", ancestor, descendant
        ).returncode
        == 0
    )


def _component_config(repo_path: Path, component: str | None) -> dict[str, object]:
    if not component:
        return {
            "dockerfile": repo_path / "Dockerfile",
            "upstream_config": repo_path / "upstream.toml",
            "release_suffix": "aio",
        }
    components_path = repo_path / "components.toml"
    if not components_path.exists():
        raise SystemExit(f"{components_path} is required for --component {component}")
    data = tomllib.loads(components_path.read_text())
    components = data.get("components", {})
    if not isinstance(components, dict) or component not in components:
        raise SystemExit(f"Unknown component in components.toml: {component}")
    config = components[component]
    if not isinstance(config, dict):
        raise SystemExit(f"Component {component} must be a table in components.toml")
    return {
        "dockerfile": repo_path / str(config.get("dockerfile", "Dockerfile")),
        "upstream_config": repo_path
        / str(config.get("upstream_config", "upstream.toml")),
        "release_suffix": str(config.get("release_suffix", "aio")),
    }


def _release_profile(repo_path: Path, profile: str) -> str:
    if profile != "auto":
        return profile
    if repo_path.name == "unraid-aio-template":
        return "semver"
    return "aio"


def _path_arg(repo_path: Path, value: Path | None) -> Path | None:
    if value is None:
        return None
    return value if value.is_absolute() else repo_path / value


if __name__ == "__main__":
    raise SystemExit(main())
