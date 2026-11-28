FROM debian:trixie-slim@sha256:8aa5a7bf5a1955bddbe7d74fcdd0b91ab30e74362a8b73af672b0481b35b44a0

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        attr ca-certificates catatonit crun fuse-overlayfs iproute2 iptables libcap2-bin netavark \
        nftables passt podman procps python3 rsync slirp4netns uidmap util-linux \
    && rm -rf /var/lib/apt/lists/*

ARG WORKER_UID
ARG WORKER_GID
RUN test "$WORKER_UID" -gt 0 && test "$WORKER_GID" -gt 0 \
    && groupadd --gid "$WORKER_GID" worker \
    && useradd --uid "$WORKER_UID" --gid "$WORKER_GID" --create-home --shell /bin/bash worker \
    && install -d -m 0700 -o worker -g worker "/run/user/$WORKER_UID" \
    && chmod 0755 /usr/bin/newuidmap /usr/bin/newgidmap \
    && setcap cap_setuid=ep /usr/bin/newuidmap \
    && setcap cap_setgid=ep /usr/bin/newgidmap

# Debian's optional ping-group sysctl cannot be changed through a Pod's read-only procfs.
RUN mkdir -p /etc/containers/containers.conf.d \
    && printf '[containers]\ndefault_sysctls = []\n' > /etc/containers/containers.conf.d/90-orbit-worker.conf

# Only trusted Dockerfile preparation uses Buildah. Orbit selects runsc for every Sandbox.
ENV BUILDAH_ISOLATION=chroot \
    XDG_RUNTIME_DIR=/run/user/${WORKER_UID}

USER worker
