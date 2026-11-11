# The container that hosts a Firecracker microVM.
#
# As with the QEMU runner, ALE does not run the VMM on the host. The container gives each
# microVM a network namespace of its own, where its tap device and egress rules live, so
# the Host needs nothing but Docker, /dev/kvm and /dev/net/tun, and nothing a sandbox does
# reaches the Host's own netfilter. The same image converts a Task's exported OCI rootfs
# into the ext4 disk Firecracker boots (mke2fs reads the tar through libarchive), so the
# Host needs no e2fsprogs either.
#
# Published under each engine version by .github/workflows/images.yml; `images/build.sh
# ale-firecracker-runner` builds a version not yet published.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN apt-get update \
    && apt-get install -y --no-install-recommends e2fsprogs iproute2 iptables libarchive13t64 \
    && rm -rf /var/lib/apt/lists/*

# The official static release and the guest kernel its CI boots (Linux 6.1 with virtio
# block, net and vsock, ext4, overlayfs, netfilter and bridging built in), both pinned.
ARG FIRECRACKER_VERSION=v1.17.0
ADD --checksum=sha256:06094a1108ae9e82aa4c23a775aa92758f53f1175d422270d9d6162cb9ade558 \
    https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-x86_64.tgz \
    /tmp/firecracker.tgz
RUN tar -xzf /tmp/firecracker.tgz -C /tmp \
    && install -m 0755 /tmp/release-${FIRECRACKER_VERSION}-x86_64/firecracker-${FIRECRACKER_VERSION}-x86_64 \
        /usr/local/bin/firecracker \
    && rm -rf /tmp/firecracker.tgz /tmp/release-*
ADD --checksum=sha256:e20e46d0c36c55c0d1014eb20576171b3f3d922260d9f792017aeff53af3d4f2 \
    https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64/vmlinux-6.1.155 \
    /opt/ale/vmlinux

COPY entrypoint.sh /usr/local/bin/ale-firecracker-entrypoint
RUN chmod 0755 /usr/local/bin/ale-firecracker-entrypoint /opt/ale && chmod 0644 /opt/ale/vmlinux

LABEL org.opencontainers.image.source="https://github.com/AgentsLastExam/ale" \
      org.opencontainers.image.description="Hosts an ALE Linux sandbox as a Firecracker microVM" \
      ale.role="firecracker-runner"

ENTRYPOINT ["/usr/local/bin/ale-firecracker-entrypoint"]
