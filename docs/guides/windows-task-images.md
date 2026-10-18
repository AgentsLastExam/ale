# Windows Task images

Each Task deploys its software and configuration through `image/run.ps1`; ALE caches the
resulting image locally and starts each episode in a fresh writable VM overlay. Follow the
[cross-platform software rules](../specs/task-authoring.md#software-assets) to decide whether
to install directly or prepare offline assets. For software requiring repackaging on Windows,
prefer MSIX; an existing suitable unattended offline installer can be reused directly.

## Define and build the image

Use an operator-provided ALE Windows base and declare it in `task.yaml`:

```yaml
spec_type: core/v1
name: my-windows-task
os: windows
image:
  kind: vm
  base_ref: /absolute/path/to/windows-base.qcow2
```

Keep software materials under `image/assets/softwares/editor_1/` and deployment scripts under
`image/`. In `run.ps1`, consume those local files, deploy the software and apply Task configuration.
The Task's materialized folder contains its build inputs; the build does not depend on a sibling
Task, a Host installation or a browser session. HF acquisition happens on the Host before build.
Check native command exit codes and follow the [Windows build rules](../specs/task-authoring.md#image).

`ale prepare TASK` boots a temporary base VM, stages `image/`, executes the script, removes build
inputs, installs ALE's guestd and Cua Driver, and exports a cached qcow2. Stable installation belongs here; only per-episode initialization
belongs in `setup/`. Subsequent runs reuse the image. Base, builder or image input changes invalidate
reuse. The current Windows upload and input hashing read whole files into Host memory; very large
assets still require bounded-memory transport and hashing work.

## Prepare an MSIX asset

1. Reuse a compatible package if available. Otherwise acquire and install the software in a
   separate preparation VM, recording the official source, version and acquisition steps.
2. Package with MSIX Packaging Tool or Windows SDK MakeAppx. Include the signature and public
   certificate needed by the chosen deployment mode; exclude credentials and signing private keys.
3. Save the package and required offline dependencies in the Task's software asset directory.
   Keep a short README with hashes, compatible base, deployment requirements and validation results.
4. Supply an unattended deployment recipe in `image/`. Use MSIX installation/registration for the
   actual agent user, or a tested native deployment recipe that reproduces required registry,
   service and dependency setup. Unpacking an arbitrary MSIX is insufficient. An overlay preserves
   Windows permissions and package restrictions; configure writable paths explicitly.

Keep Task-specific configuration outside the reusable package. Publication follows the
[asset contract](../specs/task-assets.md); repository-level software consolidation is not yet
implemented. Share software only where its license permits.

## Validate

From the ALE checkout, run against a complete Task:

```bash
uv run ale lint /absolute/path/to/task
uv run ale prepare /absolute/path/to/task
uv run ale validate /absolute/path/to/task
```

On a fresh base, verify unattended deployment with only the declared inputs and dependencies.
As the actual agent user, test required GUI/CLI workflows: create, modify, invoke child tools,
save and reopen. Verify image-cache reuse and isolation between fresh episode overlays. Build
privileges do not imply agent privileges. Keep validation evidence and qualify GUI Tasks with
the intended visual agent; see the [image contract](../specs/sandbox-image.md).
