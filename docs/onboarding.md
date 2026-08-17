# Repository Onboarding

Use `unraid-aio-template` to create the new app repo first. Then add it to `fleet.yml`.

Required manifest fields:

- `path`: local checkout path used by operator commands.
- `app_slug`: stable fleet slug.
- `workflow_name`: user-facing GitHub Actions workflow name.
- `image_name`: image path without registry, for example `wgross19/example-aio`.
- `docker_cache_scope`: GitHub Actions cache scope for the main image.
- `pytest_image_tag`: local image tag used by integration tests.
- `publish_profile`: one of `template`, `upstream-aio-track`, `changelog-version`, `dify`, `multi-component`, or `signoz-suite`.
- `release_name`: user-facing release workflow name.
- `upstream_name` and `image_description`: OCI metadata labels.
- `xml_paths`: XML/template paths watched by CI.
- `catalog_assets`: source-to-target copy rules for `awesome-unraid` sync.

Optional fields:

- `generated_template`: marks repos whose XML is generated from source data.
- `generator_check_command`: command run before XML validation.
- `checkout_submodules`: required for repos like `mem0-aio`.
- `extra_publish_paths`: paths that should trigger image publishing.
- `extended_integration`: manual extended integration test input and pytest args.
- `components`: component-aware publish lanes, used when a repo publishes more
  than one AIO-managed image.
- `upstream_components`: matrix values for component-aware upstream checks.
- `upstream_commit_paths`: files committed by an upstream monitor PR.
- `upstream_monitor`: version/digest sources used by central upstream PR automation.
- `upstream_monitor[].submodule_path`: tracked gitlink to update with the
  upstream PR, used by repos that vendor upstream sources as a submodule.
- `upstream_monitor[].submodule_ref_template`: ref or branch template used for
  the submodule checkout, for example `codex/openmemory-{version}-aio`.
- `previous_tag_command`: release-script command used as the changelog base tag.

## Playbook

Generate the starting checklist from the control plane:

```bash
python -m aio_fleet onboard-repo --repo <repo> --mode existing
python -m aio_fleet onboard-repo --repo <repo> --mode new-from-template
python -m aio_fleet onboard-repo --repo <repo> --mode rehab
python -m aio_fleet onboard-repo --repo <repo> --shape single-image
python -m aio_fleet onboard-repo --repo <repo> --shape multi-component
python -m aio_fleet onboard-repo --repo <repo> --shape submodule-backed
python -m aio_fleet onboard-repo --repo <repo> --shape destination-only
python -m aio_fleet onboard-repo --repo <repo> --shape rehab-only
python -m aio_fleet promote-rehab --repo <repo> --dry-run --format json
```

Use `existing` for a current app repo that is already structurally close to the
fleet model, `new-from-template` for a brand new repo created from
`unraid-aio-template`, and `rehab` only for neglected repos that should appear
on the dashboard without blocking active fleet validation. `nanoclaw-aio` is an
active multi-component repo now, not a rehab example.
Use `--shape` to generate the acceptance pack for the repo surface: normal
single-image apps, multi-component repos such as `nanoclaw-aio`, bundled
multi-upstream apps such as `penpot-aio`, future submodule-backed apps,
dashboard-only catalog/destination repos, and rehab-only repos that must stay
non-blocking.

1. Create the repo from `unraid-aio-template`.
2. Keep only app-specific source/runtime/template/docs/tests in the app repo.
3. Add the repo to `fleet.yml`, including Docker Hub image name, XML assets,
   release profile, and upstream monitor config.
4. Export `.aio-fleet.yml` into the app repo.
5. Validate the app repo through central policy and cleanup checks.
6. Run a central control-check dry-run against the app commit.
7. Run upstream-monitor and registry dry-runs.
8. Sync catalog assets into `awesome-unraid` with a PR when the source repo is ready.
9. Render the support-thread draft and complete CA-facing metadata review.
10. Prove `aio-fleet / required`, `Superagent Security Scan`, and `Contributor trust`
    appear on a real app PR before branch protection depends on them.

Acceptance commands:

```bash
python -m aio_fleet export-app-manifest --repo <repo> --write
python -m aio_fleet validate-repo --repo <repo> --repo-path ../<repo>
python -m aio_fleet cleanup-repo --repo <repo> --verify
python -m aio_fleet cleanup-repo --repo <repo> --fix --verify
python -m aio_fleet control-check --repo <repo> --sha <commit-sha> --event pull_request --dry-run
python -m aio_fleet release preflight --repo <repo> --sha <commit-sha> --mode transaction --format json
python -m aio_fleet upstream monitor --repo <repo> --dry-run
python -m aio_fleet registry verify --repo <repo> --sha <commit-sha> --dry-run --verbose
python -m aio_fleet sync-catalog --repo <repo> --catalog-path ../awesome-unraid --dry-run
python -m aio_fleet support-thread render --repo <repo>
python -m aio_fleet doctor
```

## Rehab Mode

`rehab` mode is intentionally planning-first and non-blocking. A rehab repo may
appear in the Fleet Command Center while staying out of active app commands
such as `validate --all`, registry verification, release publish, and upstream
monitor `--all`.

Promote a rehab repo only after:

- the local checkout is synced to `main`;
- Dockerfile, runtime wrapper, XML, README, and support docs have been audited;
- publish profile and upstream monitor strategy are explicit;
- `.aio-fleet.yml` has been exported from `fleet.yml`;
- legacy workflows/config/scripts that `aio-fleet` replaces are removed;
- central validation and cleanup verification pass;
- `aio-fleet / required`, `Superagent Security Scan`, and `Contributor trust` appear on a
  real PR.

`promote-rehab` does not mutate `fleet.yml`; it produces the acceptance pack
for moving a rehab repo into the active `repos` map. It blocks on missing local
checkout, dirty worktree, and retired shared files, and it prints the exact
manifest entry and first central validation commands needed for the promotion
PR.

## Multi-Component Repos

Use `nanoclaw-aio` as the reference when a repo publishes more than one
AIO-managed image. Declare each published image under `components`, give each
component its own cache scope, Dockerfile/context, upstream version key, release
suffix, and publish policy, then add matching `upstream_monitor` entries. The
NanoClaw helper image is `registry_only`, so it gets registry tags but does not
pretend to have its own Community Apps XML or formal release lane.

Use `penpot-aio` as the reference when several upstream images are monitored but
the repo still publishes one AIO wrapper image. In that shape, keep
`publish_profile: changelog-version`, add separate `upstream_monitor` entries
for the upstream images and digests, and do not add `components` unless the AIO
repo is intentionally split into separately published images.

## Submodule-Backed Repos

Use `mem0-aio` as the reference when a repo must build from upstream source kept
as a git submodule. Keep `checkout_submodules: true`, include the gitlink in
`upstream_commit_paths`, and declare the submodule path/ref in the matching
monitor entry. If the AIO wrapper carries fork-only patches, keep those patches
on a version-specific branch in the configured fork, then let `aio-fleet`
advance the app repo gitlink in a verified upstream PR.
