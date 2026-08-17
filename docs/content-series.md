# Public Case Study Outline

This repo is the technical artifact behind a public fleet-maintainability series.

## Part 1: Why One-Off Homelab App Repos Become Fleet Debt

Angle: each AIO repo starts small, then CI, release, template, and catalog behavior gets copied into every repo. The debt is not obvious until the same bug has to be fixed seven times.

Proof points:

- duplicated workflow lines removed from app repos;
- publish-gate behavior moved to one control-plane path;
- repo exceptions made explicit in `fleet.yml`.

## Part 2: Designing A Control Plane For Unraid AIO Containers

Angle: keep product repos independent, but centralize the shared operating model.

Proof points:

- `unraid-aio-template` stays the bootstrap source;
- app repos stay app-local;
- `awesome-unraid` stays catalog-only;
- `aio-fleet` manages policy and drift.

## Part 3: Control-Plane CI/CD, Release Gates, And Drift Detection

Angle: one GitHub App check-run is easier to govern than workflow copies spread across every app repo.

Proof points:

- `aio-fleet / required` check-run orchestration;
- manifest-driven repo metadata;
- central release, registry, Trunk, and validation commands;
- `doctor`, `validate`, `cleanup-repo`, and `status` commands.

## Part 4: Managing GitHub Repo Infrastructure With Terraform/OpenTofu

Angle: source files and GitHub-owned settings are different kinds of state.

Proof points:

- repo settings, rulesets, environments, topics, and required checks belong in IaC;
- source-file sync stays in workflow/template tooling.

## Part 5: What Stayed App-Specific And Why

Angle: good fleet management does not flatten real differences.

Proof points:

- Dify extended integration tests;
- Signoz dual image lanes;
- Mem0 submodule handling;
- generated-template checks for Dify and Infisical.
