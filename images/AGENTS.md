# Sandbox image agent guide

Read [`README.md`](README.md) and `../docs/specs/sandbox-image.md` first.

- `base/` contains foundational Sandbox images, never Task or domain images.
- `builders/` creates image artifacts; `runtimes/` hosts them during an episode.
- Keep what a base provides (agent account, desktop configuration, bootstrap guest
  integration) in sync with Provider checks and the image specification.
- Never bake provider credentials, benchmark answers or Task-specific dependencies.
- Images are published under the engine version and never overwritten; a changed image
  ships with the next engine version. Linux bases carry no guestd and no base carries Cua
  Driver: the engine installs both. The Windows base keeps only the bootstrap guestd its
  Task builds talk to. A changed image contract requires container/VM conformance coverage.
- Avoid extra bootstrap mechanisms: long-lived fixed software belongs in the image,
  Task-specific software belongs in the Task Dockerfile.

Run the narrow image build first; use `just images` only when all base images are meant
to change.
