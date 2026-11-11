# syntax=docker/dockerfile:1
# Hearthfund: one image, data in the /data volume only.
# The base is pinned by digest (the multi-arch index, so amd64 and arm64 resolve to the same tag).
ARG PYTHON_IMAGE=python:3.13-slim@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2

# ---- builder: a virtual environment with the pinned dependencies and the app
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build
# Every file pip installs is checked against a hash: the runtime dependencies (requirements.lock) and the build
# backend (requirements-build.lock). The app itself builds with --no-build-isolation, so nothing unpinned is
# downloaded.
COPY requirements.lock requirements-build.lock ./
RUN pip install --require-hashes -r requirements.lock \
 && pip install --require-hashes -r requirements-build.lock
COPY pyproject.toml README.m[d] LICENSE* ./
RUN test -f README.md || echo "# Hearthfund" > README.md
COPY src ./src
RUN pip install --no-deps --no-build-isolation . \
 && pip uninstall -y setuptools pip \
 && python -m compileall -q /opt/venv

# ---- runtime: no build tools, non-root, read-only root filesystem friendly
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev   # release images carry the version in their labels (set by the release workflow)
LABEL org.opencontainers.image.title="hearthfund" \
      org.opencontainers.image.description="Self-hosted household budget: checkbook forecast, bill tracking and bank reconciliation" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
# - the app user (fixed uid/gid 10001) and the data folder
# - no setuid/setgid programs
# - no package managers at run time: pip and setuptools leave the system Python, ensurepip goes too (its bundled
#   pip wheel carries old vendored copies of urllib3, msgpack and pkg_resources that image scanners flag), apt and
#   libapt-pkg are purged (nothing else needs them; a fix for apt need not wait for a new base image), and the dpkg
#   programs are removed (the dpkg status file stays, so image scanners can still list the OS packages)
RUN groupadd --system --gid 10001 hbudget \
 && useradd --system --uid 10001 --gid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin hbudget \
 && mkdir -p /data \
 && chown 10001:10001 /data \
 && chmod 0700 /data \
 && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
 && python -m pip uninstall -y pip setuptools wheel \
 && rm -rf /usr/local/lib/python3.*/ensurepip
# security fixes Debian has published since the pinned base image was built (zlib1g CVE-2026-76844 and
# CVE-2026-85091, 2026-11-09), applied while apt is still here; drop a package once a newer base digest carries it
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade zlib1g \
 && rm -rf /var/lib/apt/lists/*
RUN dpkg --purge --force-remove-essential --force-remove-protected apt \
      $(dpkg-query -W -f='${binary:Package}\n' 'libapt-pkg*') \
 && rm -rf /etc/apt \
 && rm -f /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config /usr/bin/apt-key /usr/bin/apt-mark \
          /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-query /usr/bin/dpkg-divert /usr/bin/dpkg-split \
          /usr/bin/dpkg-statoverride /usr/bin/dpkg-trigger /usr/bin/dpkg-maintscript-helper \
 && rm -rf /var/cache/apt /var/lib/apt/lists /var/log/apt
COPY --from=builder /opt/venv /opt/venv
COPY docker/healthcheck.py /usr/local/lib/hbudget-healthcheck.py
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HBUDGET_DATA_DIR=/data \
    HBUDGET_HOST=0.0.0.0 \
    HBUDGET_PORT=8080
USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/usr/local/lib/hbudget-healthcheck.py"]
# The entrypoint is Python, not a shell: it sets umask 077 (the database, its WAL files, the secret key and
# backups are owner-only; the app also sets it) and then replaces itself with `hbudget <command>`.
# /bin/sh stays in the image for `docker compose exec`.
ENTRYPOINT ["python", "-c", "import os, sys; os.umask(0o077); os.execvp('hbudget', ['hbudget', *sys.argv[1:]])"]
CMD ["serve"]
