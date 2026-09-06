# Two images from one build: the Orders viewer, and the Orders poller.
#
# A Node stage builds the frontend from its lockfile, a shared Python stage
# installs from uv.lock, and the two service stages take what each of them
# needs from it. The frontend's toolchain does not survive into either running
# image — only the few files it produced — and the poller carries no frontend
# at all.
#
# The two stages are separate because their rights differ. The viewer must not
# be able to write anything; the poller must not be able to serve anything.
# Sharing one image would have given each the other's surface for nothing.
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


# --- the dependency tree, installed once -----------------------------------
# Both service stages start here, so the lockfile is resolved and installed in
# a single layer that they share. Nothing application-specific belongs in this
# stage: anything copied here is copied into both images.
FROM python:3.12-slim AS deps

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


# --- the viewer ------------------------------------------------------------
FROM deps AS service

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


# --- the poller ------------------------------------------------------------
# Runs the incremental fetch on a loop, so the Order store keeps up with
# dispatch without anyone running a command. See poll.sh for the loop, and
# docker-compose.yml for how it is configured.
FROM deps AS poller

COPY get_orders.py poll.sh ./

# The poller writes to the database and to its own stdout, and to nothing
# else: poll.sh passes --no-json --no-excel, so no output directory is opened
# and no volume is needed. That is deliberate — this repository lives in
# iCloud Drive, and a process writing customer records into a synced folder
# every few minutes is a sync daemon's problem, not a durable archive.
RUN useradd --create-home --uid 10002 poller
USER poller

# No EXPOSE and no healthcheck: it listens on nothing, and "healthy" for a
# periodic job means its last run succeeded, which `docker compose logs` and
# the Order store's own contents answer better than a probe could.
CMD ["sh", "poll.sh"]
