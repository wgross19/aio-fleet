# aio-base — shared s6-overlay + hardening stage

`dub19/aio-base` centralizes the pieces that fleet app runtime images
otherwise copy-paste into their own Dockerfiles:

- the pinned, SHA-verified **s6-overlay** install (today three repos pin three
  different versions; this standardizes them on one), and
- the **apt hardening** (rewrite inherited sources to `https`, retry/timeout
  defaults, snakeoil-cert removal), exposed as the `aio-harden` helper.

It is a build-only overlay (a `scratch` image holding `/aio-overlay`), published
multi-arch by [`aio-base.yml`](../../.github/workflows/aio-base.yml) behind the
protected `registry-publish` environment.

## Consuming it from an app image

Replace the inline s6 download/verify/extract block and the apt-hardening
preamble with:

```dockerfile
FROM dub19/aio-base:s6-3.2.1.0 AS aio-base

FROM <upstream-image>
COPY --from=aio-base /aio-overlay/ /
RUN aio-harden pre \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade \
    && apt-get install -y --no-install-recommends <app deps> \
    && aio-harden post
# ... app-specific layers ...
ENTRYPOINT ["/init"]
```

`/aio-overlay` is the extracted, arch-matched s6 rootfs (`/init`, `/package`,
`/command`, `/etc/s6-overlay`, …) plus `/usr/local/bin/aio-harden`.

## Bumping s6 or the hardening

Edit `infra/aio-base/Dockerfile` (version + the three SHA256 digests) or
`aio-harden`, bump `TAG` in the workflow, and republish. App images pick up the
new overlay on their next build once they reference the new tag.

## Rollout

Migrate one repo at a time, validating each builds and boots. Current app
runtime wrappers should consume this overlay from their Dockerfiles. Small helper
or agent images can remain deliberately lightweight when they do not need s6
supervision or the shared apt hardening.
