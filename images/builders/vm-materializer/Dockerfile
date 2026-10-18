# Builds Linux VM disks: turns a Task's OCI rootfs into a bootable qcow2 (mkosi), and
# installs the ALE framework into a qcow2 named by reference (libguestfs, whose small
# appliance VM runs the disk's own programs instead of the Host; linux-image-kvm is its
# kernel).
FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cpio \
        dosfstools \
        e2fsprogs \
        grub-common \
        grub-efi-amd64-bin \
        grub-pc-bin \
        kmod \
        libguestfs-tools \
        linux-image-kvm \
        mkosi \
        mtools \
        ovmf \
        python3-pefile \
        qemu-utils \
        systemd-boot \
        systemd-ukify \
        ubuntu-keyring \
        xz-utils \
        zstd \
    && rm -rf /var/lib/apt/lists/*

COPY mkosi.conf /config/mkosi.conf
COPY 10-root.conf /config/repart.d/10-root.conf
COPY materialize.sh /usr/local/bin/ale-materialize-vm
# Ubuntu 24.04+ hosts attach an AppArmor profile to the path /usr/bin/bwrap that strips
# capabilities inside the sandbox mkosi builds in, so tar cannot restore root ownership and
# every build fails. The container is already privileged; a copy earlier on PATH is not
# matched by that host profile.
RUN mkdir -p /config/extra/etc \
    && ln -s /run/systemd/resolve/stub-resolv.conf /config/extra/etc/resolv.conf \
    && chmod 0755 /usr/local/bin/ale-materialize-vm \
    && cp /usr/bin/bwrap /usr/local/bin/bwrap

ENTRYPOINT ["/usr/local/bin/ale-materialize-vm"]
