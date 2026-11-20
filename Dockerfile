# syntax=docker/dockerfile:1
#
# Air-gapped build: every Python dependency is vendored under vendor/wheels and
# installed with --no-index.  No package index is contacted at build time.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /srv/importer

# Runtime dependencies first (cached layer).  Wheels are pure-python or
# manylinux cp311, matching this base image.
COPY vendor/wheels /srv/vendor/wheels
COPY requirements.txt ./
RUN python -m pip install --no-index --find-links /srv/vendor/wheels -r requirements.txt \
 && rm -rf /srv/vendor

COPY app ./app
COPY tools ./tools

# The seed image and the demo assets are provided as read-only mounts by
# docker-compose.yml; the app only writes DATA_DIR.
RUN useradd --system --uid 10001 --home-dir /srv/importer importer \
 && mkdir -p /data \
 && chown -R importer:importer /data /srv/importer
USER importer

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
