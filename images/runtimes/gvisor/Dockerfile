# Orbit's gVisor runtime, as gVisor's release tarball lays it out: runsc, the containerd shim
# and the sidecars runsc runs from gvisor-bin/ next to itself, all from one patched build.
#
# Nothing runs this image as a sandbox: an operator copies /gvisor onto the Host and
# registers its runsc with Docker (README.md). The entrypoint lets `docker create` take it
# and `docker run --rm <image> --version` print the build. build.sh builds the tarball with
# gVisor's own Bazel build and then this image; .github/workflows/images.yml publishes
# each version once.
FROM scratch

ADD gvisor.tar.bz2 /gvisor/

ARG GVISOR_TAG
LABEL org.opencontainers.image.source="https://github.com/AgentsLastExam/orbit" \
      org.opencontainers.image.description="gVisor runsc with Orbit's patches" \
      orbit.role="gvisor-runtime" \
      orbit.gvisor.release="${GVISOR_TAG}"

ENTRYPOINT ["/gvisor/runsc"]
