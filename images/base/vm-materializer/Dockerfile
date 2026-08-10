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
RUN mkdir -p /config/extra/etc \
    && ln -s /run/systemd/resolve/stub-resolv.conf /config/extra/etc/resolv.conf \
    && chmod 0755 /usr/local/bin/ale-materialize-vm

ENTRYPOINT ["/usr/local/bin/ale-materialize-vm"]
