# The container that hosts a virtual machine.
#
# ALE does not run `qemu-system-x86_64` on the host. This image does, and it is inherited
# rather than written: the parts that are tedious and easy to get subtly wrong — device
# permissions, the guest's bridge and its DHCP, port forwarding to the guest, signal
# handling — were already solved here, and re-solving them would buy nothing.
#
# What we change is small and deliberate: the entrypoint states what *this* framework
# expects, checks the disk chain the way our overlays are actually built, and carries no
# configuration for a benchmark we do not run. Publishing it under our own namespace is
# the other half — a run should not depend on an image someone else can move or delete.
#
# Build and publish:
#   docker build -f images/base/qemu-runner/Dockerfile -t ghcr.io/agentslastexam/ale-qemu-runner:0.1.0 images/base/qemu-runner
#   docker push ghcr.io/agentslastexam/ale-qemu-runner:0.1.0
FROM agentslastexam/ale-qemu:0.2.0

COPY entrypoint.sh /usr/local/bin/ale-qemu-entrypoint
RUN chmod 0755 /usr/local/bin/ale-qemu-entrypoint

# What the guest service listens on, so the entrypoint can say so rather than a reader
# having to know. The provider publishes this port; nothing here binds it.
ENV ALE_GUEST_PORT=7411

LABEL org.opencontainers.image.source="https://github.com/AgentsLastExam/ale" \
      org.opencontainers.image.description="Hosts an ALE guest VM under qemu/KVM" \
      ale.role="qemu-runner"

ENTRYPOINT ["/usr/local/bin/ale-qemu-entrypoint"]
