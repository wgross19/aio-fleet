# Required Checks

Use these as the control-plane map for required checks.

## App Repos

App repos should require:

- `aio-fleet / required`
- `Superagent Security Scan`
- `Contributor trust`

`aio-fleet` may still post detail checks for validation, tests, registry, and
catalog work, but those are diagnostic. Branch protection should key on the
single required control-plane check plus the two Superagent checks.

The check must be created by a GitHub App or another token with Checks write
permission. GitHub documents that check-run write access is available for GitHub
Apps, and the required permission is repository `Checks: write`.

The required check should also be pinned to the GitHub App producer. The live
branch protection API should report `aio-fleet / required` with app ID
`3565017`; `validate-github` treats any other app ID as drift.

Superagent is a blocking fleet gate. The live PR check suite reports both
`Superagent Security Scan` and `Contributor trust` from the `Superagent Security` app, app
ID `3287076`; `validate-github` should pin those contexts to that producer.

## aio-fleet

Require:

- `test`
- `infra`
- `Analyze (actions)`
- `Analyze (python)`
- `Superagent Security Scan`
- `Contributor trust`

The GitHub Actions contexts should be pinned to app ID `15368`; the standalone
`CodeQL` check reports app ID `57789`. The Superagent contexts should be pinned
to app ID `3287076`.

## awesome-unraid

Require:

- `validate-catalog`
- `CodeQL`
- `Analyze (actions)`
- `Superagent Security Scan`
- `Contributor trust`

The GitHub Actions contexts should be pinned to app ID `15368`. The Superagent
contexts should be pinned to app ID `3287076`.

## unraid-aio-template

Require the same logical gates as app repos: `aio-fleet / required`, `Security
scan`, and `Contributor trust`. The repo is public and should use the same
managed branch-protection/ruleset posture as active app repos so new AIO repos
start from a protected bootstrap baseline.

## Selected Actions

Active repos should keep GitHub Actions restricted to selected actions with SHA
pinning required. App repos should not need selected-action exceptions once
their local workflows are removed. Catalog-specific exceptions, such as
`peter-evans/create-pull-request`, should stay explicit and pinned.
