# GitHub App Automation Track

The current fleet automation uses repository secrets such as
`AIO_FLEET_BOT_TOKEN` for generated pull requests that need branch-protection
checks to run. This works, but a GitHub App is the better long-term control-plane
credential because it is scoped, auditable, installable per repository, and
rotatable without sharing a personal token.

## Future App Scope

Start with the smallest app permission set that supports the fleet jobs:

- Metadata: read
- Contents: write
- Pull requests: write
- Checks: write
- Actions: read

Only add broader administration permissions if the GitHub App later owns repo
settings through the OpenTofu layer:

- Administration: write
- Rulesets: write

Do not grant secret-read permissions. GitHub does not expose secret values, and
the fleet should only validate secret names/presence.

## Current Control-Plane Support

The control-plane workflows resolve automation credentials through
`aio_fleet.github_app`:

- If `AIO_FLEET_APP_CLIENT_ID`, `AIO_FLEET_APP_INSTALLATION_ID`, and
  `AIO_FLEET_APP_PRIVATE_KEY` are present, `aio-fleet` mints a short-lived
  installation token. `AIO_FLEET_APP_ID` remains a compatibility fallback for
  older environments.
- Generated commit paths must use that App token. Missing App credentials are a
  `credential-gap`; do not fall back to a PAT or the repository `GITHUB_TOKEN`
  for generated release/catalog commits.
- API commit payloads must not set custom `author`, `committer`, or `signature`
  fields. GitHub applies bot verification only when the commit is authored by
  the App/bot identity through the supported API or action path.

Release PR creation should not use `RELEASE_TOKEN` as a generic fallback. That
token can be valid for release/tag operations while still lacking pull-request
write access and verified bot-signing behavior. Prepare-release workflows should
fail early when the GitHub App token cannot be created.

Check-runs are stricter than release PRs. The required fleet check is created
by `aio-fleet check run`, and the long-term path is a GitHub App installation
token with Checks write access. The local `AIO_FLEET_CHECK_TOKEN` fallback is
only for controlled operator use while bringing the App online.

GitHub's Checks API requires this app-shaped path for creating check-runs:
OAuth apps and authenticated users can view checks, but creating check-runs is
the GitHub App control-plane boundary. That is why the branch-protection
migration waits until the App identity posts a real `aio-fleet / required`
check on an app PR.

Branch protection should require the `aio-fleet / required` check from the
GitHub App's app ID, plus Superagent's `Superagent Security Scan` and `Contributor trust`
checks from the Superagent Security app ID. The GitHub branch protection API
exposes this as `required_status_checks.checks[].app_id`, and `aio-fleet
validate-github` fails when a required check is present but tied to the wrong
producer. This prevents a same-name workflow or status from accidentally
satisfying the fleet gate.

Required signed commits stay enabled. Generated commits use the GitHub App
contents API with no custom author, committer, or signature fields so GitHub can
apply bot signature verification. `aio-fleet` then checks the commit API and
fails before PR creation if GitHub reports `verified=false`.

Run the signing doctor before merging or publishing generated fleet work:

```bash
python -m aio_fleet signing doctor --all --format json
```

The doctor checks App credentials, signed-commit branch protection, open
generated PR commit verification, repo-local workflow writers, local hooks, and
stray `.trunk/` overlays outside `aio-fleet`.

Do not use GitHub rebase-merge on protected AIO repos. GitHub documents that
its rebase-merge path rewrites commits and cannot sign those rewritten commits.
With required signed commits enabled, the secure merge methods are squash or
merge commit through GitHub's signed web flow, or a local signed merge pushed by
an authorized maintainer.

GHCR package publishing is separate from GitHub App check-runs. The central
publish jobs use the short-lived workflow `GITHUB_TOKEN` with `packages: write`.
If a package is associated with an app repository rather than `aio-fleet`, add
`aio-fleet` to the package's **Manage Actions access** list with Write access.

Keep `AIO_FLEET_GHCR_TOKEN` available for local/operator preflight only. Do not
route it into app validation runs or normal protected publish jobs.

The private key secret should contain the PEM text. Escaped `\n` sequences are
accepted so the value can be stored in GitHub Secrets without preserving literal
newlines.

Do not configure the GitHub App client secret for fleet automation. Client
secrets are for OAuth user-authorization flows, while Fleetbot automation uses
the App client ID, private key, and short-lived installation tokens.

## Migration Path

1. Create the GitHub App with the minimal permission set above.
2. Install it only on active fleet repos, `awesome-unraid`, and `aio-fleet`.
3. Remove generated-commit PAT fallbacks once App credentials are configured.
4. Add `AIO_FLEET_APP_CLIENT_ID`, `AIO_FLEET_APP_INSTALLATION_ID`, and
   `AIO_FLEET_APP_PRIVATE_KEY` to `aio-fleet`. Keep `AIO_FLEET_APP_ID` only as
   a temporary fallback while old workflows finish draining.
5. Add `AIO_FLEET_APP_CLIENT_ID` and `AIO_FLEET_APP_PRIVATE_KEY` to repo-local
   workflow writers that use `actions/create-github-app-token`; store the client
   ID as a repository variable and the private key as a repository secret.
6. Verify generated PRs and catalog syncs pass required checks using the app
   identity and report `pull-request-commits-verified=true`.
7. Run `aio-fleet poll --create-checks --dry-run`, then a real Sure PR check.
8. Update branch protection to require `aio-fleet / required` from the GitHub
   App app ID plus Superagent's `Superagent Security Scan` and `Contributor trust` checks.
9. Run `aio-fleet cleanup-repo --verify`, or `aio-fleet cleanup-repo --fix --verify`
   when removing known retired shared files.

The GitHub App is the generated-commit identity. PAT secrets may still exist for
registry or operator-only release edges, but not for generated PR commits.
