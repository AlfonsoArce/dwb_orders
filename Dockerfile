# The Orders viewer: one image serving one origin.
#
# A Node stage builds the frontend from its lockfile, and a Python stage
# installs from uv.lock and serves both the API and the built assets. The
# frontend's toolchain does not survive into the running image — only the few
# files it produced.
#
# The build context is an allowlist; see .dockerignore, which exists so that
# customer freight records in the working tree cannot reach a layer.

# --- the frontend ----------------------------------------------------------
# Pinned to the major version in .nvmrc, so the host and the image agree.
FROM node:26-alpine AS frontend

WORKDIR /build

# The lockfile first, so a change to the application source does not reinstall
# the dependency tree.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# Type-checks as well as bundles: `npm run build` is `tsc --noEmit && vite build`,
# so a type error fails the image rather than shipping in it.
RUN npm run build


# --- the service -----------------------------------------------------------
FROM python:3.12-slim AS service

# uv resolves nothing here: it installs exactly what uv.lock already pins.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY dwb/ ./dwb/
COPY viewer.py ./
COPY --from=frontend /build/dist ./frontend/dist

# Nothing in here needs to write anything: the viewer's whole point is that it
# cannot, and the database enforces the rest of that claim.
RUN useradd --create-home --uid 10001 viewer
USER viewer

# Bound to every interface *inside the container*; docker-compose publishes it
# on 127.0.0.1 only, so nothing about this is reachable from the network.
EXPOSE 8000
CMD ["python", "viewer.py", "--host", "0.0.0.0", "--port", "8000"]
