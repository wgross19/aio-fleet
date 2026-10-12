# syntax=docker/dockerfile:1@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
# checkov:skip=CKV_DOCKER_3: s6-overlay requires root init so cont-init scripts can prepare state before services drop privileges
# checkov:skip=CKV_DOCKER_8: s6-overlay entrypoint must start as root so init scripts can prepare the filesystem state before dropping privileges

ARG DEBIAN_IMAGE=debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5

# checkov:skip=CKV_DOCKER_8:s6 is PID 1 and must start as root; it drops privileges to honcho (99:100) for runtime services
FROM ${UV_IMAGE} AS uv

#checkov:skip=CKV_DOCKER_7:base images are digest-pinned, not 'latest'
FROM ${DEBIAN_IMAGE}

# Upstream Honcho pinned release. The fleet monitor drives the release tag via
# the HONCHO_VERSION ARG (version_key). The build itself stays pinned to
# HONCHO_GIT_SHA; HONCHO_VERSION is the discoverable release label only.
ARG HONCHO_VERSION=v3.3.0
ARG HONCHO_GIT_SHA=ad3839db4133e282da20ded84125c93056b88a40
ARG HONCHO_REPO=https://github.com/plastic-labs/honcho.git
ARG S6_OVERLAY_VERSION=3.2.1.0
ARG POSTGRES_MAJOR=17
ARG HONCHO_USER=honcho
ARG HONCHO_UID=99

LABEL org.opencontainers.image.title="honcho-aio" \
      org.opencontainers.image.source="${HONCHO_REPO}" \
      org.opencontainers.image.revision="${HONCHO_GIT_SHA}" \
      org.opencontainers.image.version="${HONCHO_VERSION}" \
      com.honcho-aio.service="honcho"

ENV DEBIAN_FRONTEND=noninteractive \
    HONCHO_HOME=/var/lib/honcho \
    PATH="/opt/honcho/.venv/bin:/usr/lib/postgresql/${POSTGRES_MAJOR}/bin:/usr/local/bin:${PATH}" \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=300000 \
    S6_KEEP_ENV=1

# trunk-ignore hadolint/DL3002: s6-overlay requires root init so cont-init scripts can prepare state
# hadolint ignore=DL3002
USER root
# hadolint ignore=DL3008,DL4006
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    openssl \
    xz-utils \
    gosu \
    python3 \
    redis-server \
  && install -d -m 0755 /etc/apt/keyrings \
  && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    postgresql-${POSTGRES_MAJOR} \
    postgresql-${POSTGRES_MAJOR}-pgvector \
    postgresql-client-${POSTGRES_MAJOR} \
  && curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" -o /tmp/s6-noarch.tar.xz \
  && curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz" -o /tmp/s6-arch.tar.xz \
  && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
  && tar -C / -Jxpf /tmp/s6-arch.tar.xz \
  && rm -f /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz \
  && useradd --system --uid ${HONCHO_UID} --gid users --home-dir /var/lib/honcho --create-home honcho \
  && mkdir -p /opt/honcho /var/lib/honcho /data/postgres /data/redis /run/postgresql \
  && chown -R honcho:users /opt/honcho /var/lib/honcho \
  && chown -R postgres:postgres /data/postgres /run/postgresql \
  && chown -R redis:redis /data/redis \
  && rm -rf /var/lib/apt/lists/*

# Copy uv from the pinned uv image into a builder layer.
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /opt/honcho
# Upstream v3.1.1 requires Python >=3.13 (bookworm apt ships 3.11), so uv
# auto-downloads a managed CPython at build. Keep that interpreter in a
# world-readable path: uv defaults to $HOME/.local/share/uv (=/root/...,
# mode 0700 on bookworm), and runtime services exec the venv as gosu honcho,
# which cannot traverse /root. Without this the venv python fails to exec and
# honcho-api/deriver never start (health never binds).
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN git clone --filter=blob:none "${HONCHO_REPO}" /tmp/honcho-src \
  && git -C /tmp/honcho-src checkout --detach "${HONCHO_GIT_SHA}" \
  && test "$(git -C /tmp/honcho-src rev-parse HEAD)" = "${HONCHO_GIT_SHA}" \
  && cp -a /tmp/honcho-src/. /opt/honcho/ \
  && rm -rf /tmp/honcho-src \
  && uv sync --frozen --no-install-project --no-group dev \
  && chmod -R a+rX /opt/uv/python \
  && chown -R honcho:users /opt/honcho \
  && printf '%s\n' '#!/bin/sh' \
       'exec /opt/honcho/.venv/bin/fastapi run --host 0.0.0.0 --port 8000 /opt/honcho/src/main.py' \
     > /usr/local/bin/honcho-api \
  && chmod 755 /usr/local/bin/honcho-api

COPY rootfs/ /
RUN chmod 755 /etc/cont-init.d/* /etc/services.d/*/run /usr/local/bin/*

EXPOSE 8000
VOLUME ["/data/postgres", "/data/redis", "/var/lib/honcho"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=10 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/init"]
