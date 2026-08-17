# AGENTS.md

This repository is the fleet control plane for the wgross19 Unraid AIO repos.

## Intent

- Keep shared CI/release/drift policy out of individual app repos.
- Keep app repos focused on app runtime, Docker, XML, docs, and tests.
- Treat `fleet.yml` as the canonical manifest for repo metadata and exceptions.

## Rules

- Prefer reusable workflows and generated thin callers over copied workflow YAML.
- Do not move app-specific runtime logic into this repo unless it is genuinely shared.
- Keep `unraid-aio-template` as the repo bootstrap template.
- Keep `awesome-unraid` as the downstream Community Apps catalog.
- Any publish-related change must preserve integration-test gates.

## Public Dashboard Privacy Invariant

- Treat the Fleet Update Dashboard issue body, including hidden/base64 JSON state, as public.
- Treat any manifest repo whose `public` field is not exactly `true` as private.
- Never collect or embed private repo GitHub identifiers, PR/issue details, release tags, commit SHAs, Docker Hub/GHCR tags, registry failures, cleanup paths, local branch state, or command strings containing private identifiers in the dashboard.
- Private repo dashboard rows may include only the manifest repo key and redacted status labels such as `private-skipped`.
- Any new dashboard state field must either be generated only for public repos or passed through a whitelist redaction helper before `render_dashboard()`.
- Add or update a regression test that decodes the hidden dashboard state any time dashboard registry, release, activity, cleanup, workflow, or future Fleetbot report fields are added.
