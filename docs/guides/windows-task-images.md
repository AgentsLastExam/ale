# Windows Task images and reusable software MSIX assets

Use this guide when a Windows Task needs software prepared before the evaluated agent starts.
The workflow is: reuse or contribute an offline software asset, deploy it through the Task's
`image/run.ps1`, cache the resulting local qcow2, then run each episode in an independent overlay.
Vendor acquisition happens once in a packaging VM; Task builds and runs do not repeat vendor login.

The [Task authoring contract](../specs/task-authoring.md#windows-software-assets-author-workflow)
owns author responsibilities. The [image specification](../specs/sandbox-image.md) owns build,
cache, user and VM behavior. This guide adds recipes, not new ALE commands or manifest fields.

## What is available

| Component | Current status |
|---|---|
| Windows `image/run.ps1`, local qcow2 cache and fresh episode overlays | Implemented in ALE |
| UE 5.5.4 offline MSIX asset, deployed as ordinary native files | Qualified locally; findings below |
| Shared software library on a private HF dataset | Recommended storage workflow; repository and access must be supplied by its operator |
| Automatic shared-software lookup, Host fetch/mirror lifecycle, package contribution from Brew | Not implemented; use explicit Host preparation and the maintainer's publication workflow |
| `ale assets` | Manages Task-local stage assets, not the shared software library |

No published HF location for the UE experiment is asserted here. Inspect the supplied library
before assuming a package is available. A missing catalog entry is a packaging task, not a reason
to embed vendor authentication into every Task. An operator must supply the Windows base, asset
access and any account/license access needed for acquisition.

## Start a Windows Task

Use an ALE-compatible Windows base with sufficient usable C: capacity, desktop and guest
integration. A larger qcow2 alone does not guarantee more free space inside Windows: a Recovery
partition after C: can prevent expansion. Use the operator's qualified base; do not put disk or
boot repair into Task scripts. A packaging VM may have SDKs and signing tools that never enter
the final Task image.

```text
tasks/my_windows_task/
  task.yaml
  instruction.md
  image/
    run.ps1                 # entry point: acquire verified bytes, deploy, configure
    software.json           # Task-owned immutable pins and deployment inputs
    deploy.ps1              # reviewed software recipe, copied from the library
    start-app.ps1           # only if the application needs a launcher
    assets/                 # optional Task-specific initial files / small offline inputs
  oracle/run.ps1
  verify/run.ps1
```

For example, complete this manifest with the operator's actual base path and Task-specific
resources. Implement verification and oracle normally; this is not a complete benchmark Task.

```yaml
spec_type: core/v1
name: my-windows-task
os: windows
image:
  kind: vm
  base_ref: /absolute/operator/path/windows-base.qcow2
resources:
  cpus: 8
  memory_mb: 24576
  sudo: false
network:
  mode: block
artifacts:
  - 'C:\ALE\Output'
```

Image installation runs elevated independently of `resources.sudo`. Choose the runtime privilege
required by the actual Task and test it; an elevated build is not evidence that a limited user can
run the software. Keep stable installation, dependencies, permissions and initial inputs in
`image/`. Use `setup/` only for genuinely per-episode state. Do not modify the base in place or
submit an exploratory VM snapshot instead of reproducible scripts.

## Reuse an existing package

Read the library entry's manifest, deployment script and validation record before downloading.
Match vendor version, architecture, Windows version, dependencies, writable paths, entry points,
required privileges and deployment mode to the Task. Keep Task-specific settings in its `image/`;
do not fork a large package merely to change a project path or editor preference.

An illustrative Task-owned `image/software.json` for the verified UE bytes is below. Replace the
repository, commit and URL placeholders with supplied values; they are not provisioned endpoints.
ALE does not parse this file. The Host downloader and Task script consume it explicitly.

```json
{
  "name": "ue",
  "version": "5.5.4",
  "packaging_revision": "r1",
  "deployment": "extracted-native",
  "repo_id": "YOUR_ORG/software-assets",
  "revision": "REPLACE_WITH_40_CHARACTER_HF_COMMIT",
  "filename": "ue/5.5.4/windows-x64/r1/UE55Mutable.msix",
  "bytes": 39614165149,
  "sha256": "4068861053476c70e3aed4187185d9f8dcb4b9ef716cde144ec27b86840f8086",
  "url": "https://YOUR_BUILDER_REACHABLE_MIRROR/UE55Mutable.msix"
}
```

### Acquire bytes on the Host

For a private HF dataset, use Host-side `huggingface_hub` authentication with read access. Run this
from the ALE checkout, replacing the Task path. It downloads the pinned version into the existing
HF cache, checks its size and streams its SHA-256 calculation; it does not install anything.

```bash
uv run python - /absolute/path/to/tasks/my_windows_task/image/software.json <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys
from huggingface_hub import hf_hub_download

asset = json.loads(Path(sys.argv[1]).read_text())
if not re.fullmatch(r"[0-9a-f]{40}", asset["revision"]):
    raise ValueError("Pin an immutable HF commit")
if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
    raise ValueError("Pin the package SHA-256")
path = Path(hf_hub_download(
    repo_id=asset["repo_id"], repo_type="dataset",
    revision=asset["revision"], filename=asset["filename"],
))
with path.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if path.stat().st_size != asset["bytes"] or digest != asset["sha256"]:
    raise ValueError("Software asset integrity mismatch")
print(path)
PY
```

Provide those verified bytes through a builder-reachable internal artifact endpoint, or stage
small inputs under `image/assets/`. Currently Windows context upload reads each file into Host
memory, so do not stage a tens-of-GB MSIX in `image/`; the UE test used a narrow temporary Host
HTTP mirror and a streaming guest download. Bind such a mirror only to the required build
network, serve only approved artifacts, and stop it after the build. Guest `localhost` is not the
Host. Supplying/recreating this endpoint is an explicit operator step, not an automatic ALE hook.

Keep the URL free of credentials. The HF token, vendor session and signing private key stay
outside the Task and VM. Copy reviewed deployment scripts into Task source from the same pinned
library revision; do not fetch an unversioned script and execute it during the build. Changing a
pin or recipe changes the image context and triggers a new build. A moving URL without a checked
digest does not establish reproducibility.

### Deploy in `image/run.ps1`

The following streaming pattern was used for the large UE asset. Supply a Task-local
`deploy.ps1` that accepts `-PackagePath`, follows the selected mode below, and throws on failure.
That parameter is an example script interface, not an ALE API. Add Task-specific initial files
and configuration after deployment, before the image is saved.

```powershell
$ErrorActionPreference = 'Stop'
$asset = Get-Content "$PSScriptRoot\software.json" -Raw | ConvertFrom-Json
$package = "$PSScriptRoot\software.msix"
$request = [Net.HttpWebRequest]::Create([uri]$asset.url)
$request.Timeout = 600000
$request.ReadWriteTimeout = 600000
$response = $request.GetResponse()
try {
    $source = $response.GetResponseStream()
    try {
        $target = [IO.File]::Open($package, 'Create', 'Write', 'None')
        try { $source.CopyTo($target, 4MB) } finally { $target.Dispose() }
    } finally { $source.Dispose() }
} finally { $response.Dispose() }
if ((Get-Item $package).Length -ne $asset.bytes) { throw 'Asset size mismatch' }
$stream = [IO.FileStream]::new($package, [IO.FileMode]::Open,
    [IO.FileAccess]::Read, [IO.FileShare]::Read, 1MB, [IO.FileOptions]::SequentialScan)
try { $hash = (Get-FileHash -InputStream $stream -Algorithm SHA256).Hash }
finally { $stream.Dispose() }
if ($hash -ne $asset.sha256) { throw 'Asset SHA-256 mismatch' }
& "$PSScriptRoot\deploy.ps1" -PackagePath $package
if (-not $?) { throw 'Software deployment failed' }
Remove-Item $package
```

Inside the deployment recipe, check every native exit code; PowerShell's
`$ErrorActionPreference` alone does not make native failures terminate. Handle documented
success/reboot codes explicitly, suppress reboots, and normalize a successful recipe's exit
status to zero. Copy required runtime files out of the temporary image context. ALE removes that
context and owns shutdown/export. The build budget and reboot restrictions are in
[Task authoring](../specs/task-authoring.md#image).

| Deployment mode | What the recipe must do | What to verify |
|---|---|---|
| Installed MSIX | Establish the expected signing trust, install package dependencies, use `Add-AppxPackage` for the actual runtime user and create supported activation/alias entry points | User registration, activation, writes and every required child-tool path |
| Extracted native payload | Use an explicitly qualified flat payload; deploy to an ordinary directory, install dependencies, create necessary registry/service/COM integration and grant required user access | Ordinary executable launch, writes, all child tools and persistence after a fresh boot |

`Add-AppxPackage` registers an application for a user; merely provisioning a package or registering
it under a different administrator does not prove the agent can activate it. Its protected files
do not become writable because the VM disk has an overlay. Native mutable MSIX directories have
version, capability and privilege constraints. Test them; do not assume a manifest switch makes
the package unrestricted. PSF is an optional packaging-time compatibility fix, and app attach is
an optional attach/register delivery mechanism. Neither is necessary when the software has
already been deployed into a Task image.

Extraction also has limits: `tar.exe -xf` does not apply MSIX VFS mappings, registry virtualization,
COM declarations, services or licensing. A flat engine tree with a tested native recipe can work;
arbitrary captured MSIX packages cannot be treated as portable ZIPs. Check the expected signing
certificate before importing trust; do not ship its private key or weaken WindowsApps permissions.

## Create and contribute a missing package

Use the same workflow for a missing software/version and for a shared compatibility fix:

1. **Acquire in a disposable packaging VM.** Use the official installer or supported acquisition
   flow. Interactive login/verification, when required, belongs here. Record source, vendor
   version, architecture, installer hashes and steps in `package.ps1` and the entry's README;
   identify any human acquisition step rather than pretending the whole acquisition is unattended.
2. **Capture the required installation.** Use the Microsoft MSIX Packaging Tool for an installer
   that changes files/registry, or Windows SDK MakeAppx for a known staged payload and authored
   `AppxManifest.xml`. Include required executables, assets and declarations. Inventory external
   runtimes, SDKs, drivers and services; decide which the deployment recipe must install separately.
   Exclude account sessions, credentials, signing keys, unrelated projects and packaging tools.
3. **Package and sign.** Pin SDK/tool versions, identity, publisher, architecture and package version.
   The manifest publisher must match the signing certificate. Publish only the public certificate;
   keep private signing material in the packaging environment. Record final size and SHA-256 after
   signing. A successful MakeAppx exit is not complete payload/deployment validation.
4. **Write the reusable deployment recipe.** Choose installed-MSIX or qualified native deployment.
   It must work on the declared clean base without the acquisition account or Launcher session.
   Record prerequisites, user registration, writable paths, entry points and compatibility settings.
   Dependencies supplied by a packaging VM must not silently become undeclared requirements.
5. **Qualify before contributing.** Follow the fresh-image checks below using the exact signed
   bytes. Keep scripts and results, not just a screenshot or a successful installation log.
6. **Submit a new immutable revision.** Submit the package, certificate, manifest, packaging and
   deployment scripts, and validation evidence together. Record known limits. Use a new revision
   for shared payload/recipe changes, even when the vendor version is unchanged. After publication,
   pin the resulting commit in consuming Tasks; do not overwrite an old tested entry in place.

For an already prepared staging directory containing `AppxManifest.xml` and its referenced files,
the Windows SDK packaging/signing commands are:

```powershell
$ErrorActionPreference = 'Stop'
$sdk = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64'
if (-not $env:ALE_SIGNING_THUMBPRINT) { throw 'Select the packaging signing certificate' }
& "$sdk\makeappx.exe" pack /d .\staging /p .\Software.msix /o
if ($LASTEXITCODE -ne 0) { throw 'MakeAppx failed' }
& "$sdk\signtool.exe" sign /fd SHA256 /sha1 $env:ALE_SIGNING_THUMBPRINT .\Software.msix
if ($LASTEXITCODE -ne 0) { throw 'Signing failed' }
& "$sdk\signtool.exe" verify /pa .\Software.msix
if ($LASTEXITCODE -ne 0) { throw 'Signature verification failed' }
Get-FileHash .\Software.msix -Algorithm SHA256
```

The selected certificate must already be installed in the signing user's certificate store, and
its trust must be configured for verification. For large packages, MakeAppx's `/f` mapping file
can avoid duplicating the source tree; `/nc` disables compression at a size cost. Preserve the
tool output and validate payload integrity before distributing a new package. See Microsoft's
[packaging overview](https://learn.microsoft.com/en-us/windows/msix/packaging-tool/tool-overview)
and [MakeAppx reference](https://learn.microsoft.com/en-us/windows/msix/package/create-app-package-with-makeappx-tool).

### Library layout and publication

A private organization HF dataset can hold the shared library. For example:

```text
software-assets/
  ue/5.5.4/windows-x64/r1/
    README.md
    Software.msix
    Software.cer
    manifest.json
    package.ps1
    AppxManifest.xml
    deploy.ps1
    validation/             # checks, logs, relevant screenshots and RunLocks
```

The library manifest should record vendor and packaging versions, source/acquisition recipe,
file sizes/hashes, certificate identity, deployment mode, supported OS, prerequisites, user
privileges, writable paths, launch entry points and qualification results/limits. Small auxiliary
configuration or launcher scripts used by the recipe belong in the same entry. Library layout and
manifest are maintainer conventions; ALE does not enforce a catalog schema.

An authorized publisher can upload a reviewed contribution directory as an HF PR with the existing
Python client. Use an existing private dataset and only the intended contribution files:

```bash
uv run python - YOUR_ORG/software-assets /absolute/path/to/contribution ue/5.5.4/windows-x64/r1 <<'PY'
import sys
from huggingface_hub import HfApi

repo, folder, destination = sys.argv[1:]
api = HfApi()
if not api.repo_info(repo, repo_type="dataset").private:
    raise ValueError("Software library must be private")
result = api.upload_folder(
    repo_id=repo, repo_type="dataset", folder_path=folder, path_in_repo=destination,
    commit_message=f"Add qualified software asset: {destination}", create_pr=True,
)
print(result.pr_url)
PY
```

Software distribution access must match the vendor's license. The dataset needs sufficient
private storage quota; HF currently documents a 500 GB per-file maximum. See
[storage limits](https://huggingface.co/docs/hub/storage-limits),
[tokens](https://huggingface.co/docs/hub/security-tokens), and
[downloads pinned to a revision](https://huggingface.co/docs/huggingface_hub/guides/download).

Brew authors retain their existing write/publication boundaries: prepare Task scripts and
approved asset outputs, keep contribution notes/evidence in the author-run directory, and hand
the candidate to the library maintainer when direct publication is not authorized. Do not use
`ale assets publish` for this library or claim a contribution is published until its repository,
commit and review result exist. The consuming Task's unrelated large fixtures still use normal
[Task asset management](../specs/task-assets.md).

## Build and qualify the actual Task image

From an ALE checkout, run the ordinary commands against the completed Task:

```bash
uv run ale lint /absolute/path/to/tasks/my_windows_task
uv run ale prepare /absolute/path/to/tasks/my_windows_task
uv run ale validate /absolute/path/to/tasks/my_windows_task
```

Then run the intended visual agent with the operator's configured Harness/Gateway. Qualification
must cover the workflow the Task requires, not only installation:

- Boot a fresh VM from the image. Confirm the actual agent user's token, application access,
  dependencies and intended runtime network policy. Do not rely on the packaging account.
- Create a new document/project at a path absent from the package. Exercise internal helpers,
  compilation or plugins the Task actually uses. Save, close normally, reopen and verify output.
- Start another fresh overlay and confirm the first one's file, application and registry changes
  are absent. Shut down and clean up disposable VMs through ALE.
- Stop the asset endpoint and repeat preparation/run to prove local image reuse. Record base,
  package, recipe and prepared-image identities, complete RunLocks and actual GUI/CLI outcomes.
- Confirm final images contain no acquisition credentials or private keys. Keep public launch
  instructions and Task initial data available, and protected verifier/oracle assets out of images.

The image build cache is whole-build reuse, not Dockerfile-style per-command layers. First-build
transfer, hashing, unpacking and disk export can be substantial; warm Task runs must not reinstall
software. Application startup may still populate shader or document caches. A successful image
probe is distinct from Task validation and an evaluated visual-agent result.

## UE 5.5.4: tested deployment decision

The local qualification on Windows 10 Pro 19045 used a 39,614,165,149-byte MSIX with SHA-256
`4068861053476c70e3aed4187185d9f8dcb4b9ef716cde144ec27b86840f8086`. Its signature and
211,895 payload entries were validated. It is an installed/prepared engine asset, not a claim of
bit-identical reconstruction from a new Launcher download.

Installed mutable MSIX allowed elevated engine writes and C++ builds through a packaged .NET
alias. However, GUI project conversion called bundled `dotnet.exe` directly and received
`Access is denied`. This was a real execution-path failure beyond write permissions; the exact
Windows enforcement mechanism was not established. That route is not qualified for general UE use.

The passing recipe extracts the same verified flat engine payload to
`C:\ALE\Software\UE_5.5`, installs official UE prerequisites outside the package, and grants
the user Modify access to the engine and project directories. It installs any missing .NET
Framework SDK, registers the engine under `HKCU\Software\Epic Games\Unreal Engine\Builds`,
and uses ordinary executable/desktop entry points. No UE Appx registration, PSF or source patch
is used. Required MSVC/SDK versions belong in the recipe and its UBT configuration.

The tested configuration pins the MSVC 14.38.33130 directory and Windows SDK 10.0.22621.0,
disables both UBA executors, and preserves normal project generation and shared PCH. UE 5.5.4
does not accept `-NoLiveCoding` as its disabling switch: the launcher uses `-LiveCoding=false`
and sets `bEnabled=False` in `Engine\Config\Windows\WindowsEditorPerProjectUserSettings.ini`.
That Windows file overrides the base setting. This correction resolved the observed Live Coding
shutdown wait and was included in a full rebuild, not only a manually fixed runtime VM.

The rebuilt image passed fresh solution generation, UHT and C++ DLL compilation, GUI in-place
conversion/rebuild, non-elevated editor scene load/save, normal exit 0, cache reuse without the
asset server, and two-overlay file/registry isolation. Installation took about 19 minutes in the
builder; the standalone task disk was about 69 GB. Each cold boot took about 32 seconds after
about 85 seconds of preparation/base identity checks on that Host.

These results use an experimental prerequisite base, 8 CPUs / 24 GiB at runtime,
`resources.sudo: true` with the actual desktop editor verified non-elevated, and D3D11 software
rendering. Hardware GPU, Live Coding, arbitrary plugins and all UE workflows are not qualified.
Some build-time Microsoft prerequisites still used network sources. A new Task must include or
install those dependencies and test its own required privileges/features; copying only the engine
files or reusing the sample compiler settings is not complete qualification.
