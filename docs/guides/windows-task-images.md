# Windows Task images

Prepare software once as reusable MSIX assets. Each Task deploys the required assets and its
configuration through `image/run.ps1`; ALE caches the resulting image locally and starts each
episode in a fresh writable VM overlay.

## Define the image

Use an operator-provided ALE Windows base and declare it in `task.yaml`:

```yaml
spec_type: core/v1
name: my-windows-task
os: windows
image:
  kind: vm
  base_ref: /absolute/path/to/windows-base.qcow2
```

Keep installation scripts, pinned software references and Task configuration under `image/`.
`run.ps1` is the entry point; helper scripts and a file such as `software.json` are ordinary
Task-owned files, not additional ALE manifest fields.

`ale prepare TASK` boots a temporary base VM, executes the image script, removes build inputs,
and exports a locally cached qcow2. Changes to the base, builder or image inputs invalidate
reuse. Stable installation belongs in `image/`; only per-episode initialization belongs in
`setup/`. Build execution and runtime privileges are separate: configure and test the agent's
required permissions. See the [image contract](../specs/sandbox-image.md).

## Reuse software from the library

1. Find an asset matching the software version, architecture, Windows version and required
   workflow. Read its deployment recipe, dependencies and validation results.
2. Pin the library repository, immutable commit, package path, size and SHA-256 in Task source.
   Copy the reviewed deployment recipe into `image/` and adjust Task-specific settings there.
3. Fetch private assets on the Host, keeping credentials outside the Task and VM. Use a private
   Hugging Face dataset and `huggingface_hub.hf_hub_download` with the pinned revision, or an
   equivalent versioned store. Deliver large files through a build-accessible artifact endpoint;
   Windows context upload currently reads each file into Host memory.
4. Have `run.ps1` verify the asset's size/hash, install dependencies, deploy the software, and
   configure user access, launch entry points and initial Task files. Check command exit codes
   and follow the [Windows build rules](../specs/task-authoring.md#image).

Use the asset's tested deployment mode: MSIX installation/registration for the actual agent user,
or a qualified native deployment recipe. Native deployment must reproduce any required registry,
service and dependency setup; unpacking an arbitrary MSIX is insufficient. A VM overlay isolates
changes but does not override Windows permissions or package restrictions.

## Create and contribute a missing asset

1. Acquire and install the software in a separate packaging VM. Record the official source,
   version and acquisition steps, including any one-time interactive login.
2. Create an MSIX with MSIX Packaging Tool or Windows SDK MakeAppx, sign it, and record the final
   size/hash. Keep credentials and signing private keys out of the package.
3. Supply an unattended deployment recipe and test it on a fresh base as the intended user.
   Record external dependencies, writable paths, launch methods and compatibility limits.
4. Contribute the package, public certificate, manifest, packaging/deployment scripts and
   validation evidence together. Use a new packaging revision for shared changes; keep
   Task-specific settings in the consuming Task.

Organize library entries by `software/version/architecture/packaging-revision/`. A private HF
repository can hold these files and review contributions through PRs. Publish within the author's
existing permissions, or hand the prepared contribution to the maintainer. Consumers pin the
published commit. Software access and distribution must respect its license.

## Validate and reuse

From the ALE checkout, run these commands against a complete Task:

```bash
uv run ale lint /absolute/path/to/task
uv run ale prepare /absolute/path/to/task
uv run ale validate /absolute/path/to/task
```

In fresh VMs, check the required GUI/CLI workflows as the actual agent user: create new work,
modify it, invoke required child tools, save, close and reopen. Check image-cache reuse with the
asset source unavailable, and confirm changes do not leak between episode overlays. Keep
RunLocks and validation evidence, and qualify GUI Tasks with the intended visual agent.

Subsequent runs reuse the local Task image; they do not repeat software acquisition or
installation. Rebuild when the pinned assets or image recipe change.

## Integration boundary

ALE implements Windows script builds, local image caching and episode overlays. Shared software
library discovery, HF fetching and contribution publication are explicit author/operator steps;
they are not automatically orchestrated by ALE. The operator supplies the base and library access.
`ale assets` manages [Task-local stage assets](../specs/task-assets.md), separately from this library.
