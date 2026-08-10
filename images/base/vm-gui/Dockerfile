FROM ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y \
        ca-certificates \
        curl \
        dbus \
        grub-efi-amd64-bin \
        initramfs-tools \
        iproute2 \
        linux-image-generic \
        nftables \
        python3 \
        python3-pil \
        python3-xlib \
        sudo \
        systemd \
        systemd-boot-efi \
        systemd-resolved \
        systemd-sysv \
        ubuntu-desktop \
        udev \
        xdotool \
    && rm -rf /var/lib/apt/lists/*

ARG NODE_VERSION=v24.15.0
ARG NODE_SHA256=472655581fb851559730c48763e0c9d3bc25975c59d518003fc0849d3e4ba0f6
RUN curl -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md \
    && rm /tmp/node.tar.xz

RUN id -u user >/dev/null 2>&1 || useradd --create-home --shell /bin/bash user

COPY packages/ale-run/src/ale/run/guestd/ /opt/ale/guestd/
COPY images/base/vm-gui/ale-guestd.service /etc/systemd/system/ale-guestd.service
COPY images/base/vm-gui/20-wired.network /etc/systemd/network/20-wired.network
COPY images/base/vm-gui/gdm-custom.conf /etc/gdm3/custom.conf
COPY images/base/vm-gui/nftables.conf /etc/nftables.conf
COPY images/base/vm-gui/image.json /etc/ale/image.json
COPY images/base/vm-materializer/10-root.conf /usr/lib/repart.d/10-ale-root.conf

RUN mkdir -p /efi /home/user/.config \
    && touch /home/user/.config/gnome-initial-setup-done \
    && chown -R user:user /home/user \
    && chown -R root:root /opt/ale \
    && chmod -R go-rwx /opt/ale \
    && chmod 0644 /etc/systemd/system/ale-guestd.service \
    && kernel_version="$(basename /usr/lib/modules/*)" \
    && cp "/boot/vmlinuz-${kernel_version}" "/usr/lib/modules/${kernel_version}/vmlinuz" \
    && systemctl enable ale-guestd.service systemd-networkd.service \
        systemd-resolved.service nftables.service gdm3.service \
    && systemctl set-default graphical.target \
    && : > /etc/machine-id

LABEL ale.user="user" \
      ale.gui="true" \
      ale.vm_base_release="0.1.0"
