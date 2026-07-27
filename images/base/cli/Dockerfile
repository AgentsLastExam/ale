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
        bash ca-certificates curl git jq python3 python3-venv sudo unzip \
    && rm -rf /var/lib/apt/lists/*

# Node, and only Node. Several autonomous harnesses ship as npm packages, and a runtime
# is a property of the platform — the kind of thing an image is for.
#
# The agents themselves are not baked. Which agent a run uses, and at which version, is a
# property of the experiment: an image that answers it makes every run on that image share
# the answer whether or not anyone chose it, and `latest` — which is what this used to
# install — makes two runs a month apart two different agents compared as one. The harness
# installs the pinned version at the start of the episode, while the sandbox still has a
# network; the declared policy binds the agent, not the preparation.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# The account the agent runs as. An agent with root can change the network policy, the
# clock and the guest service itself, so a result obtained that way is not reproducible —
# the unprivileged user is what makes a measurement mean something.
#
# The home directory is where the agent's workspace lives, which is why files staged
# there are owned correctly without anything having to grant it afterwards.
ARG AGENT_USER=user
RUN useradd --create-home --shell /bin/bash "${AGENT_USER}"

# The guest service: standard library only, so the system interpreter is enough. Owned by
# root and unreadable to the agent, which should not be able to read the protocol driving
# its own sandbox.
COPY packages/ale-run/src/ale/run/guestd/ /opt/ale/guestd/
RUN chown -R root:root /opt/ale && chmod -R go-rwx /opt/ale

# Only the framework's own namespace. Task data goes wherever each task declares, and the
# run's scratch directory is created per episode from the run configuration.
RUN mkdir -p /ale/kits /ale/store && chown -R "${AGENT_USER}" /ale

# What this image is, so the engine reads rather than guesses. See
# docs/specs/sandbox-image.md.
LABEL ale.user="user"

# Nothing to run, so something has to hold the sandbox open. An image that starts its own
# services declares them here instead; either way the command is the image's to choose.
CMD ["sleep", "infinity"]
