# GitHub Infrastructure

This directory is the OpenTofu/Terraform home for GitHub-owned fleet state.

It is intentionally separate from source-file sync. Use it for repository
settings, topics, branch protections, required checks, Actions allowlists,
vulnerability alerts, and declared secret/variable names. Do not store secret
values here.

## Local State

v1 uses local OpenTofu state. State files and local `.tfvars` files are ignored
by git; `.terraform.lock.hcl` is tracked so everyone uses the same provider
selection.

```bash
cd infra/github
tofu init
cp repos.tfvars.example repos.tfvars
tofu plan -var-file=repos.tfvars
```

## Adoption

`imports.tf` declares imports for the public active fleet:

- `aio-fleet`
- `awesome-unraid`
- `unraid-aio-template`
- `sure-aio`
- `simplelogin-aio`
- `khoj-aio`
- `mem0-aio`
- `infisical-aio`
- `penpot-aio`
- `dify-aio`
- `signoz-aio`
- `nanoclaw-aio`

Run `tofu plan` once after `tofu init`; OpenTofu will adopt those resources into
local state. `unraid-aio-template` is public and managed with the same branch
protection and required-check posture as the active app repos.

## Managed State

The module currently manages:

- public repo metadata, topics, homepage, and basic feature toggles
- `main` branch protection and required status checks
- signed commits, conversation resolution, review requirements, and strict checks
- merge method policy; rebase-merge stays disabled because it conflicts with
  required signed commits
- selected GitHub Actions allowlists
- SHA pinning for selected actions
- vulnerability alerts through `github_repository_vulnerability_alerts`
- the protected `aio-fleet` `registry-publish` environment used for live
  registry credentials
- declared secret and variable names as outputs only

Secret values are not managed. `AIO_FLEET_BOT_TOKEN` is declared for
`awesome-unraid`, and the CLI can verify that the secret exists. The
`registry-publish` environment should hold the narrow Docker Hub publish token
as `DOCKERHUB_PUBLISH_TOKEN`; OpenTofu manages the required-reviewer gate, not
the secret value.

## Action Allowlist Model

The selected-actions allowlist uses:

```text
wgross19/aio-fleet/.github/workflows/aio-*.yml@*
```

GitHub SHA pinning remains enabled, so callers must still use a full commit SHA.
This avoids per-repo allowlist churn every time `aio-fleet` publishes a new
workflow commit while preserving the security property that reusable workflow
calls are pinned.

## Required Check Source

App repos should require `aio-fleet / required`, `Superagent Security Scan`, and
`Contributor trust`. The policy records the GitHub App ID that must produce each
check. `validate-github` checks `required_status_checks.checks[].app_id`, not
only the context name, so a same-name workflow cannot satisfy branch protection
by accident.

Known required-check producers:

- GitHub Actions: `15368`
- CodeQL: `57789`
- wgross19 AIO Fleet: `3565017`
- Superagent Security: `3287076`

The `github_branch_protection` provider resource only supports name-based
contexts, so app-bound required checks are enforced through the
`github_repository_ruleset.trusted_required_checks` ruleset. Branch protection
still owns the other branch controls such as signed commits and review policy.
