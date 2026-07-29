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
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq python3 python3-venv sudo unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node, and only Node. Several autonomous harnesses ship as npm packages, and a runtime
# is a property of the platform — the kind of thing an image is for.
#
# From nodejs.org's own tarball, pinned and checksummed, rather than a third-party apt
# repository. NodeSource's `setup_22.x` — which this used to pipe into a shell — began
# answering 403, and an image that cannot be rebuilt is not reproducible. It also puts the
# same version in every image regardless of what the base distribution happens to ship:
# jammy's `nodejs` is 12.x, which no current agent will run on.
#
# The agents themselves are not baked. Which agent a run uses, and at which version, is a
# property of the experiment: an image that answers it makes every run on that image share
# the answer whether or not anyone chose it. The harness installs the pinned version at
# the start of the episode, while the sandbox still has a network — the declared network
# policy binds the agent, not the preparation.
ARG NODE_VERSION=v24.15.0
ARG NODE_SHA256=472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6
RUN set -eux; \
    curl -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz"; \
    echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

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

# What this image is, so the engine reads rather than guesses. See
# docs/specs/sandbox-image.md.
LABEL ale.user="user"

# Nothing to run, so something has to hold the sandbox open. An image that starts its own
# services declares them here instead; either way the command is the image's to choose.
CMD ["sleep", "infinity"]
