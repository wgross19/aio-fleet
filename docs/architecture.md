# AIO Fleet Architecture

`aio-fleet` is the control plane for the wgross19 Unraid AIO portfolio.

It does not replace the existing source-of-truth repos:

- `unraid-aio-template` remains the bootstrap template for new app repos.
- App repos remain product/runtime repos with their Dockerfile, rootfs, XML, tests, and docs.
- `awesome-unraid` remains the Community Apps-facing catalog and icon repository.
- `aio-fleet` owns fleet policy, shared workflow behavior, validation, and drift reporting.

## Control-Plane Layers

- `export-app-manifest` renders the future app-local `.aio-fleet.yml` from the
  central `fleet.yml` entry.
- `standards reconcile` turns manifest, cleanup, GitHub policy, and release
  drift into one ordered conformance queue; write mode only applies safe local
  manifest/cleanup fixes.
- `poll` scans active repos for open PR heads and current `main` commits.
- The control-plane workflow turns poll output into a per-repo matrix, so PR
  checks and `main` publishes run independently while still using the same
  central policy code.
- `upstream monitor` scans manifest-declared upstream providers, refreshes
  Dockerfile version/digest pins when configured, and opens app repo PRs for
  human review. Generated PR branches must be committed through the verified
  writer; unsigned commits are rejected before branch protection can block them
  later.
- `fleet-dashboard update` maintains one central `aio-fleet` issue that shows
  upstream updates, PR links, required-check state, commit verification state,
  safety review evidence, deferred runtime-smoke policy, registry/release
  placeholders, alert configuration warnings, and next actions.
- `control-check` runs central validation/test/publish steps from `aio-fleet`
  and can post the final required check-run back to the app commit.
- `check run` renders or upserts the required `aio-fleet / required` check-run
  for an app commit. The check-run external ID is
  `<repo>:<sha>:<policy-hash>` so reruns update the matching policy result
  instead of creating duplicate required checks.
- The end-state branch protection target is the required GitHub App check named
  `aio-fleet / required` plus `Superagent Security Scan` and `Contributor trust`.
  Detail checks can remain informational.
- `registry verify/publish`, the scheduled `Registry Audit` workflow,
  `release status/prepare/publish`, central app test dependency installation,
  and `trunk run` provide the Python-driven control-plane jobs.
- `cleanup-repo --verify` and `cleanup-repo --fix` are the guardrails that app
  repos no longer carry local workflows, Trunk config, git-cliff config,
  upstream scripts, release shims, shared test dependency files, or copied
  community-health boilerplate.
- `alert doctor` and `alert test` verify low-noise heartbeat/webhook routing
  without moving notification wiring back into app repos.

GitHub-owned state and source-owned state stay separate:

- OpenTofu manages public GitHub-owned state: repository settings, branch protections, topics, descriptions, selected action allowlists, vulnerability alerts, and declared Actions variables/secrets names. v1 uses local state and keeps `unraid-aio-template` documented/manual because private-repo branch protection access is blocked by current API access.
- `sync-catalog` moves manifest-declared XML/icon assets into `awesome-unraid`, refuses unpublished XML, and supports icon-only staged launches.
- App runtime surfaces stay app-local until there is a proven shared abstraction.

## Why This Shape

The fleet is many similar repos with real app-specific exceptions. A monorepo would make Community Apps packaging, release provenance, and app-specific ownership worse. Pure copy/paste keeps every repo independent but makes every CI or release-policy correction multiply across the fleet.

This control-plane model keeps app repos independent while moving repeat policy
into one tested place.
