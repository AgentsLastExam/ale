# sandbox-base-cli — the headless sandbox image.
#
# Two things are baked in on purpose. The guest service, because every exec, file
# transfer and observation goes through it and copying it per episode would be waste.
# And the Claude Code CLI, because a default run should need no network at all: an
# image that installs its agent at episode start cannot run under a deny-all policy,
# and its agent version is whatever npm served that morning rather than something the
# image digest pins.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ALE_HOME=/ale

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq python3 python3-venv unzip \
    && rm -rf /var/lib/apt/lists/*

# Node is the Claude Code runtime; NodeSource keeps it current without a PPA dance.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# The guest service: standard library only, so the system interpreter is enough.
COPY packages/ale-run/src/ale/run/guestd/ /opt/ale/guestd/

# Only the framework's own namespace. Task data goes wherever each task declares, and
# the run's scratch directory is created per episode from the run configuration.
RUN mkdir -p /ale/kits /ale/store && chmod -R 0777 /ale
CMD ["sleep", "infinity"]
