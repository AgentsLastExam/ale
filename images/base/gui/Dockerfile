# sandbox-base-gui — the desktop sandbox image.
#
# Built from the previous framework's Linux image rather than from scratch. That image
# is the exported rootfs of the virtual machine its tasks were authored against, so it
# already carries the desktop, Chrome, the X tooling and the fonts those tasks expect.
# Rebuilding an equivalent would mean rediscovering years of small decisions, and any
# difference would show up as a task that passes in one place and fails in another.
#
# What it lacks is our guest service, so that is what this adds.
FROM agentslastexam/ale-ubuntu22-docker:latest

# The base image runs as an unprivileged user, which is how its tasks expect to run.
# Root is borrowed only long enough to place files outside the home directory.
USER root

# Standard library only, so the image's own Python 3.10 is enough — which is the point:
# guest interpreters belong to the image, not to us.
COPY packages/ale-run/src/ale/run/guestd/ /opt/ale/guestd/

# The framework's own namespace. Task data goes wherever each task declares.
RUN mkdir -p /ale/kits /ale/store && chmod -R 0777 /ale /opt/ale

USER user
CMD ["sleep", "infinity"]
