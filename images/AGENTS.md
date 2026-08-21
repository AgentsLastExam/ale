# Sandbox image agent guide

Read [`README.md`](README.md) and `../docs/specs/sandbox-image.md` first.

- `base/` contains foundational Sandbox images, never Task or domain images.
- `builders/` creates image artifacts; `runtimes/` hosts them during an episode.
- Keep the image-declared agent user, GUI capability, guest integration and labels in
  sync with Provider checks and the image specification.
- Never bake provider credentials, benchmark answers or Task-specific dependencies.
- A changed image contract requires container/VM conformance coverage and a content-tag
  review in `.github/workflows/images.yml`.
- Avoid extra bootstrap mechanisms: long-lived fixed software belongs in the image,
  Task-specific software belongs in the Task Dockerfile.

Run the narrow image build first; use `just images` only when all base images are meant
to change.
