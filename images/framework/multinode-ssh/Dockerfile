# Orbit's OpenSSH for Sandboxes of several nodes, under /openssh: the framework layer of a
# Task with such a Sandbox copies it to /opt/orbit-ssh, where it is built to run, and each
# node's sshd and the agent's ssh between the nodes run from there
# (packages/orbit-run/src/orbit/run/guestd/nodes.py). Nothing runs this image.
ARG OPENSSH=10.2p1

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS openssh

ARG OPENSSH
ARG OPENSSH_SHA256=ccc42c0419937959263fa1dbd16dafc18c56b984c03562d2937ce56a60f798b2
# Without OpenSSL, which ed25519 keys between Orbit's own ends do not need: smaller, and
# every binary static, so it runs on any image. Without shadow passwords: sshd then never
# reads /etc/shadow, so an account useradd left locked there, as most images' are, still
# logs in with its key, the only way in.
RUN apk add --no-cache build-base linux-headers zlib-dev zlib-static curl \
    && curl -fsSLo /tmp/openssh.tar.gz \
        "https://cdn.openbsd.org/pub/OpenBSD/OpenSSH/portable/openssh-${OPENSSH}.tar.gz" \
    && echo "${OPENSSH_SHA256}  /tmp/openssh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/openssh.tar.gz -C /tmp \
    && cd "/tmp/openssh-${OPENSSH}" \
    && ./configure --prefix=/opt/orbit-ssh --sysconfdir=/etc/ssh \
        --with-privsep-path=/var/empty --with-privsep-user=nobody \
        --without-openssl --without-shadow --with-ldflags=-static \
        --disable-lastlog --disable-utmp --disable-utmpx --disable-wtmp --disable-wtmpx \
        --disable-pkcs11 --disable-security-key \
    && make -j"$(nproc)" \
    && mkdir -p /out/bin /out/sbin /out/libexec \
    && install -s ssh scp sftp ssh-keygen /out/bin/ \
    && install -s sshd /out/sbin/ \
    && install -s sshd-session sshd-auth sftp-server /out/libexec/

FROM scratch

COPY --from=openssh /out /openssh/

ARG OPENSSH
LABEL org.opencontainers.image.source="https://github.com/AgentsLastExam/orbit" \
      org.opencontainers.image.description="A static OpenSSH for Orbit Sandboxes of several nodes" \
      orbit.role="framework" \
      orbit.openssh.version="${OPENSSH}"
