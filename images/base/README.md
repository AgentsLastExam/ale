# Base images

ALE publishes foundational Sandbox images, not Task-specific environments.

| Image | Purpose |
|---|---|
| `cli/` | unprivileged command-line agent runtime |
| `gui/` | container runtime with a desktop |
| `vm-gui/` | OCI source for a bootable desktop VM |
| `vm-materializer/` | converts final VM OCI content to qcow2 |

Task repositories extend one of these images with their fixed dependencies and inputs.
The image contract, identity rules and required labels are defined in
[`../../docs/specs/sandbox-image.md`](../../docs/specs/sandbox-image.md).

```bash
just images
```

Publishing is handled by `.github/workflows/images.yml`; do not add Task content or
provider credentials to these images.
