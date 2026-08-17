# Unraid App Factory

Central control plane for building, validating, releasing, and publishing Unraid AIO application images.

This repository is bootstrapped from the public AIO fleet framework. It is intentionally initialized with an empty `fleet.yml`. Add your own app repositories as they are created.

## Architecture

- `unraid-aio-template` bootstraps one app repository.
- App repositories own their Dockerfile, runtime wrapper, XML, tests, and app documentation.
- `aio-fleet` owns shared validation, release, registry, upstream, and catalog policy.
- `awesome-unraid` owns Community Applications-facing XML and assets.

## First setup

```bash
uv sync --extra dev
uv run aio-fleet doctor
uv run aio-fleet validate-template-common --all
uv run aio-fleet status --catalog-path ../awesome-unraid
```

## Onboarding flow

1. Create an app repository from `unraid-aio-template`.
2. Add the app to `fleet.yml`.
3. Export `.aio-fleet.yml` into the app repository.
4. Run central repository and template validation.
5. Build and test the image.
6. Publish only after the required checks pass.
7. Synchronize the validated XML and assets into `awesome-unraid`.

The initial repository contains framework code only. It does not contain copied application manifests, app release history, or upstream app entries.
