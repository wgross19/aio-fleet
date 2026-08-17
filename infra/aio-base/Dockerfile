# syntax=docker/dockerfile:1@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
# checkov:skip=CKV_DOCKER_2: this is a build-only overlay image with no runtime; it has no entrypoint or healthcheck of its own
# checkov:skip=CKV_DOCKER_3: the overlay carries no user-facing runtime; consumers set their own USER

# Shared s6-overlay + apt-hardening stage for the AIO fleet.
#
# The pinned s6-overlay version and its SHA256 digests live here ONCE instead of
# being copy-pasted (on three different versions) into every app Dockerfile. App
# images consume it as a build stage:
#
#   FROM wgross19/aio-base:s6-3.2.1.0 AS aio-base
#   FROM <upstream>
#   COPY --from=aio-base /aio-overlay/ /
#   RUN aio-harden pre && apt-get update && apt-get -y dist-upgrade \
#       && apt-get install -y --no-install-recommends <app deps> && aio-harden post
#   ENTRYPOINT ["/init"]
#
# `/aio-overlay` is the extracted, arch-matched s6-overlay rootfs (/init,
# /package, /command, /etc/s6-overlay, ...) plus /usr/local/bin/aio-harden.
ARG S6_OVERLAY_VERSION=3.2.1.0
ARG S6_NOARCH_SHA256=42e038a9a00fc0fef70bf0bc42f625a9c14f8ecdfe77d4ad93281edf717e10c5
ARG S6_X86_64_SHA256=8bcbc2cada58426f976b159dcc4e06cbb1454d5f39252b3bb0c778ccf71c9435
ARG S6_AARCH64_SHA256=c8fd6b1f0380d399422fc986a1e6799f6a287e2cfa24813ad0b6a4fb4fa755cc

FROM debian:trixie-slim AS build
ARG S6_OVERLAY_VERSION
ARG S6_NOARCH_SHA256
ARG S6_X86_64_SHA256
ARG S6_AARCH64_SHA256
ARG TARGETARCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils && \
    rm -rf /var/lib/apt/lists/*
RUN mkdir -p /aio-overlay && \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}" && \
    curl -fsSL -o /tmp/s6-noarch.tar.xz "${base}/s6-overlay-noarch.tar.xz" && \
    echo "${S6_NOARCH_SHA256}  /tmp/s6-noarch.tar.xz" | sha256sum -c - && \
    tar -C /aio-overlay -Jxpf /tmp/s6-noarch.tar.xz && \
    case "${TARGETARCH}" in \
      amd64) s6_arch="x86_64"; s6_sha="${S6_X86_64_SHA256}" ;; \
      arm64) s6_arch="aarch64"; s6_sha="${S6_AARCH64_SHA256}" ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fsSL -o /tmp/s6-arch.tar.xz "${base}/s6-overlay-${s6_arch}.tar.xz" && \
    echo "${s6_sha}  /tmp/s6-arch.tar.xz" | sha256sum -c - && \
    tar -C /aio-overlay -Jxpf /tmp/s6-arch.tar.xz && \
    rm -f /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz
COPY aio-harden /aio-overlay/usr/local/bin/aio-harden
RUN chmod +x /aio-overlay/usr/local/bin/aio-harden

FROM scratch
LABEL org.opencontainers.image.source="https://github.com/wgross19/aio-fleet" \
      org.opencontainers.image.title="AIO Fleet base overlay" \
      org.opencontainers.image.description="Pinned s6-overlay + hardening overlay shared by the AIO fleet images"
COPY --from=build /aio-overlay /aio-overlay
