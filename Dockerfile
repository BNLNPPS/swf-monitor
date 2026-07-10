# =============================================================================
# Dockerfile for swf-monitor  —  Django 4.2 / Daphne ASGI
# =============================================================================
# Build context: the swf-monitor repository root.
#
# The image clones the sibling swf-common-lib from GitHub so the container is
# fully self-contained (no host-mount of sibling repos required at runtime).
# =============================================================================

# --------------- build stage: install deps, collect static -------------------
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev libffi-dev git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Clone swf-common-lib (shared utilities used by the logging subsystem).
# Pin to main; override with --build-arg SWF_COMMON_REF=<branch|tag> if needed.
ARG SWF_COMMON_REF=main
RUN git clone --depth 1 --branch "${SWF_COMMON_REF}" \
        https://github.com/BNLNPPS/swf-common-lib.git /build/swf-common-lib

# Install swf-common-lib first (it is a dependency of swf-monitor).
RUN pip install --no-cache-dir /build/swf-common-lib

# Install swf-monitor's own Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install packages that are referenced in INSTALLED_APPS / settings but absent
# from requirements.txt (they ship as separate PyPI packages).
RUN pip install --no-cache-dir django-mcp-server django-oauth-toolkit uvicorn

# Copy the full project and install it as a package.
COPY . /build/swf-monitor
RUN pip install --no-cache-dir -e /build/swf-monitor

# Clone and install swf-epicprod (production applications installed into the
# monitor runtime; provides the pcs package in INSTALLED_APPS). Same
# self-contained pattern as swf-common-lib above.
ARG SWF_EPICPROD_REF=main
RUN git clone --depth 1 --branch "${SWF_EPICPROD_REF}" \
        https://github.com/BNLNPPS/swf-epicprod.git /build/swf-epicprod \
    && pip install --no-cache-dir /build/swf-epicprod

# Collect static files.  We set only the variables that settings.py requires at
# import time; the real values come from the environment at runtime.
RUN SECRET_KEY="build-placeholder" \
    DB_HOST=localhost \
    DJANGO_LOGGING_MODE=none \
    python /build/swf-monitor/src/manage.py collectstatic --noinput || true

# --------------- runtime stage: slim image -----------------------------------
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Bring over installed packages and collected static files from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/swf-monitor /app
COPY --from=builder /build/swf-common-lib /opt/swf-common-lib

WORKDIR /app/src

# Entrypoint handles migrations + server start.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "swf_monitor_project.asgi:application"]
