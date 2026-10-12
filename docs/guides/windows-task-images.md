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
`run.ps1` is the entry point. Pins can be constants in the script; helper scripts and a separate
manifest are optional. ALE requires no software-specific configuration format.

`ale prepare TASK` boots a temporary base VM, executes the image script, removes build inputs,
and exports a locally cached qcow2. Changes to the base, builder or image inputs invalidate
reuse. Stable installation belongs in `image/`; only per-episode initialization belongs in
`setup/`. Build execution and runtime privileges are separate: configure and test the agent's
required permissions. See the [image contract](../specs/sandbox-image.md).

## Reuse software from the library

1. Find a compatible software version in the library and read its deployment instructions.
2. Copy the reviewed deployment script into `image/`, pin the package URL to an immutable HF
   commit, and record its SHA-256. Adjust Task-specific settings locally.
3. In `run.ps1`, download the package, verify the hash, run its deployment script, and apply
   Task configuration. Check native command exit codes and follow the
   [Windows build rules](../specs/task-authoring.md#image).

For directly accessible assets, use an ordinary HF download URL:
`https://huggingface.co/datasets/ORG/software-assets/resolve/COMMIT/APP/VERSION/Software.msix`.
Replace the placeholders with the actual repository, commit and file path. A private repository
requires authentication; the current builder does not inject HF credentials. For private assets,
fetch on the Host with `huggingface_hub.hf_hub_download` and provide the bytes to the builder.
Keep tokens out of Task source and images. Large files can use an existing internal download
endpoint; Windows context upload currently reads each file into Host memory.

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
4. Contribute the package, public certificate, packaging/deployment scripts and a README with
   dependencies, hashes and validation results. Keep Task-specific settings in the consuming Task.

Use one HF dataset with entries under `software/version/`. Record supported CPU architectures
in the README; separate files or directories are needed only when multiple builds exist.
An x64-only library can state that once. Use HF commits to version package/script changes;
a separate packaging-revision number is unnecessary. Installed MSIX updates must still follow
Windows package-version rules. Contribute through the repository's existing review/publication
workflow, then pin the published commit in Tasks. Distribution must respect the software license.

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

Authors use ordinary files, download/install scripts and HF tools. These steps can be automated
without adding an ALE software catalog, dependency resolver or publication API. ALE executes the
Task's image script, caches its image and starts episode overlays. The operator supplies the base
and library access; private-asset delivery still needs to be arranged.
`ale assets` manages [Task-local stage assets](../specs/task-assets.md), separately from this library.
