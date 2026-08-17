# Release Model

The fleet keeps release source ownership in the app repos, but release mechanics
move to `aio-fleet`.

App repos publish from `main` through the scheduled/manual `aio-fleet`
control-plane poll after required validation passes. Poll runs fan out per repo
so one heavy multi-arch build does not block unrelated app checks. Formal
changelog entries and GitHub Releases remain release-driven, not automatic for
every merge.

## App Repos

Most app repos use one of two publish profiles:

- `upstream-aio-track`: wrapper tags follow `<upstream>-aio.<revision>`.
- `changelog-version`: wrapper tags follow the changelog version exactly.

Every normal `main` publish emits Docker Hub and GHCR tags for:

- `latest`;
- the upstream version, such as `0.7.0`;
- `sha-<commit>`.

Formal release publishes add the exact changelog release tag, such as
`0.7.0-aio.1`. If upstream stays on `0.7.0` but the wrapper, image hardening,
runtime, or template changes need a new package, the next formal release is
`0.7.0-aio.2`. Unraid templates keep using the Docker Hub `latest` tag, so an
image rebuild still changes the remote digest even when the upstream version tag
does not change.

Publish jobs push and verify Docker Hub plus GHCR tags. Docker Hub remains the
template/catalog-preferred image reference; GHCR is a second registry surface
for package availability and operator fallback. Central GHCR publishing uses
the protected job's short-lived `GITHUB_TOKEN` with `packages: write`. If GHCR
returns `permission_denied: write_package`, grant the `aio-fleet` repository
write access under the package's **Manage Actions access** settings.

Publish jobs still require:

- push to `main`;
- a publish-related change;
- successful integration tests.

Upstream bumps are initiated centrally with `aio-fleet upstream monitor`. The
monitor reads provider and digest rules from `.aio-fleet.yml`, updates
version/digest pins when configured for PR strategy, and opens an app repo PR for
human review. Generated upstream commits must be verified/signed. If the writer
cannot produce a verified commit, the branch update fails instead of leaving a
PR that branch protection will later reject. Notify-only monitors never open
PRs; they appear in the fleet dashboard until a human decides whether the
packaged app path is affected. After an upstream PR merges, normal
control-plane validation and publish rules apply.

The same control-plane run that opens or updates an upstream PR also queues it
for central validation (a publish-disabled poll-check target via `aio-fleet
workflow upstream-poll-targets`), so the `aio-fleet / required` check completes
in that run instead of waiting for the best-effort hourly poll cron, which
GitHub frequently delays or drops. Only the monitor's own trusted, signed PRs
are queued this way and every queued target is publish-disabled, so the
protected registry-publish gate is never reached automatically — publishing
stays gated and operator-driven.

`aio-fleet registry publish` and `aio-fleet registry verify` compute the tag set
from `.aio-fleet.yml` and the release commit. Docker Hub tag verification uses
the Docker Hub tag API so post-push checks do not consume manifest-pull quota;
GHCR verification continues to use `docker buildx imagetools inspect`.

Publish builds attach BuildKit provenance and SBOM attestations by default via
`docker buildx build --attest=type=provenance,mode=max --attest=type=sbom`.
Registry verification still checks the expected image tags, while attestation
presence gives downstream tooling a stable package-safety surface to inspect.
`registry publish` verifies the expected tag set first and skips the push when
all tags are already present; pass `--force` only for intentional republish
work.

Run the registry preflight before publish or cleanup work that depends on live
credentials:

```bash
python -m aio_fleet registry preflight --mode publish --format json
python -m aio_fleet registry preflight --mode cleanup --image wgross19/sure-aio-alpha --check-delete-scope --format json
```

Local publish preflight requires `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, and
`AIO_FLEET_GHCR_TOKEN`, then checks the current Docker Hub `/v2/auth/token`
flow before an expensive build starts. The GitHub control plane instead logs in
inside the protected `registry-publish` environment with
`DOCKERHUB_PUBLISH_TOKEN` and the job-scoped `GITHUB_TOKEN`, then runs publish
code with only a preauthenticated `DOCKER_CONFIG`. App validation and
long-running Python control code never receive the raw registry tokens. Cleanup
preflight requires a separate `DOCKERHUB_DELETE_TOKEN`; it should not fall back
to the normal publish token for real tag cleanup because tag deletion needs
Docker Hub delete/admin permission. The delete-scope probe targets a random
nonexistent tag so it can distinguish a missing tag from a token that
authenticates but cannot delete.

GitHub prerelease publishing is guarded by the same control-check attestation.
`publish-github-prereleases` requires a matching control report or
`--expected-sha`, refuses dirty app checkouts, and fails if app `HEAD` no longer
matches the release SHA. The workflow still resets and cleans `app-repo` before
this step, but the CLI guard is the release-token boundary.

## Dispatch-driven formal releases

A formal release is two control-plane dispatches plus the protected-environment
approval — no hand-assembled `gh release create` or catalog PR:

1. `mode=release-prepare` with `repo` (and optional `publish_component`) runs
   `release prepare` and opens a correctly-subjected `chore(release): <ver>` PR.
   The same run queues that PR for validation (publish-disabled poll-check
   target), so it does not wait on the poll cron. Review and merge it.
2. A publish dispatch (`mode=control-check`, `event=push`, `publish=true`) from
   the merged `main` now completes the whole release inside the protected
   `registry-publish` environment: `Publish registry images` pushes the floating
   plus `vX`/`vX-aio.N` tags, `Publish GitHub release` runs `release publish`
   (idempotent — updates or skips an existing release, no-ops when no formal
   release is due, and `github_prerelease` components stay on the prerelease
   step), and `Sync catalog` opens an `awesome-unraid` PR for the published
   `<Changes>`.

The aio GitHub Release and catalog sync are therefore part of the publish path,
not separate manual steps. The release-transaction wrapper below remains the
allowlist-gated planner for `--write` execution; the dispatch flow above runs the
same underlying `release publish` / `sync-catalog` commands directly behind the
protected environment.

Release transactions are the operator-facing wrapper around planning,
preflight, publish, and catalog sync. Use them before dispatching a publish:

```bash
python -m aio_fleet release preflight --repo <repo> --component <component> --mode transaction --format json
python -m aio_fleet release transaction --repo <repo> --component <component> --sha <release-sha> --dry-run
```

`--write` mode is intentionally allowlist-only. A repo or component must carry
`release_transaction.autopilot: true` in `fleet.yml`, and the transaction still
blocks on checkout drift, missing credentials, missing delete-scope readiness,
missing required-check attestation, submodule policy mismatches, and release
metadata drift before any privileged publish step can run.

The `Registry Audit` workflow runs read-only verification for every active repo
on a schedule. Scheduled runs report missing Docker Hub or GHCR tags in the job
summary without blocking unrelated control-plane checks; manual runs can set
`fail_on_missing` to make missing tags fail the workflow.

## Alerting

Alerting is centralized in `aio-fleet`; app repos stay notification-free.
`AIO_FLEET_KUMA_PUSH_URL` drives a Uptime Kuma push heartbeat for fleet health.
`AIO_FLEET_ALERT_WEBHOOK_URL` receives low-noise JSON digests for failures,
missing registry tags, blocked release readiness, and upstream update PRs.
Successes update the Kuma heartbeat but do not send webhook messages unless the
event is an explicit recovery.

Central release commands:

```bash
python -m aio_fleet fleet-dashboard update --dry-run
python -m aio_fleet alert doctor
python -m aio_fleet alert test --dry-run
python -m aio_fleet release status --repo sure-aio
python -m aio_fleet release prepare --repo sure-aio --dry-run
python -m aio_fleet release transaction --repo sure-aio --component aio --sha <release-sha> --dry-run
python -m aio_fleet registry preflight --mode publish --format json
python -m aio_fleet release publish --repo sure-aio --dry-run
python -m aio_fleet registry verify --all --format json
python -m aio_fleet registry verify --repo sure-aio --sha <release-sha>
```

`release prepare` generates a temporary git-cliff config from `aio-fleet`,
updates `CHANGELOG.md`, and renders XML `<Changes>` from the release notes.
App-local `cliff.toml`, `scripts/release.py`, and
`scripts/update-template-changes.py` are retired.

## Generated Templates

Generated-template repos run their generator check before XML validation. This prevents the source repo from publishing a stale Community Apps XML file that later breaks catalog sync.

## Signoz

`signoz-aio` remains component-aware:

- the AIO image and agent image have separate publish lanes;
- both components have upstream monitor entries;
- component path changes decide which image is publish-related;
- agent integration tests build against the matching AIO backend image.
- release and publish commands stay component-aware for `signoz-aio` and
  `signoz-agent`.

This is an explicit fleet exception, not copied hidden logic.

## Catalog Sync

App repos sync CA-facing XML and icon assets into `awesome-unraid` by opening/updating a PR. The catalog repo remains the public CA source of truth.
