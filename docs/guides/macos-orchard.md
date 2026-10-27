# macOS Tasks through Orchard

Use an Orchard controller and ARM64 Tart worker to run each Task in a fresh macOS VM.
Supply an image meeting the [macOS image requirements](../specs/sandbox-image.md#what-a-macos-image-needs).
Supported capabilities and cleanup behavior follow the
[sandbox contract](../specs/sandbox-image.md#runtime-behavior).

## Install and start locally

On Apple Silicon, run the setup script from the ALE checkout and leave it running:

```bash
uv run scripts/setup_macos.py
```

Then, in another shell:

```bash
uv run scripts/setup_macos.py --check
source "$HOME/.local/share/ale/macos/credentials.env"
```

Setup pins Orchard **0.55.0** and Tart **2.41.1**, verifies release checksums, and starts
an isolated controller and worker with private state and TLS configuration. Existing
services and system trust settings are unchanged. The worker is a child of the setup
command, which stays in the foreground: macOS's Local Network privacy lets a program started
from a Terminal or SSH login reach the VMs only while it descends from that login, and a
worker left behind fails every VM with `no route to host`. Ctrl-C or closing the login stops
the worker; run setup again to restart it. An unattended worker needs another way past that
check, such as Orchard's `--user` option for a worker started as root. To reuse installed binaries at these
versions, pass `--orchard-path` and `--tart-path`. Signature failures require a trusted,
working binary; setup does not bypass macOS security checks.

State defaults to `~/.local/share/ale/macos`; use `--state-dir` to choose another directory.
Keep the generated `credentials.env` private. Initialization is repeatable; rerun it
after reboot because services have no login/startup registration. An existing cluster
can skip local setup and use the connection configuration below.

## Configure the connection and image

Local setup writes `run.toml` in its state directory. For an existing cluster, create
a private run configuration:

```toml
[vm]
provider = "orchard"

[vm.orchard]
endpoint = "https://orchard.example.test"
username_env = "ALE_ORCHARD_USERNAME"
password_env = "ALE_ORCHARD_PASSWORD"
python_path = "/opt/homebrew/bin/python3"
provision_timeout_sec = 1800
boot_timeout_sec = 180
# ca_file = "/absolute/path/to/trusted-ca.pem"
# server_hostname = "orchard.example.test"
# gateway_host = "ale-host.example.test"
# driver_path = "/absolute/guest/path/to/cua-driver"
# socket_path = "/absolute/guest/path/to/authorized-driver.sock"

[artifacts]
collect = "host"
```

Set the named credential variables in the Host environment. TLS verification is required;
use `ca_file` for a private CA. The current integration is validated with ALE and the
Tart worker on the same Mac; remote-worker deployments have not been validated.
When ALE and the Tart worker run on the same Mac with
default NAT networking, the Provider discovers the Host address from the VM's IPv4
route, and agents reach the Gateway at `127.0.0.1:7412` inside the VM. No Gateway setting
is needed at setup.
For another network layout, supply a reachable address in the run configuration or with
`uv run ale run TASK --set vm.orchard.gateway_host=ale-host.example.test`.
Add the selected Harness and model settings to the run configuration;
Codex CLI, Claude Code, Grok Build, OpenClaw CLI, computer-use, oracle and nop support
macOS. The `computer-use` Harness needs an Anthropic-compatible Gateway and a model
endpoint that accepts its computer tool schema. ALE appends the dialect route to
`gateway.base_url`: use the origin without a trailing `/v1` for an OpenAI-compatible endpoint.

Orchard selects the worker for each VM from the requested runtime and resources.
If readiness fails, check TLS/authentication, worker
scheduling, ARM64/Tart support and heartbeat freshness (180 seconds). Busy workers can
queue work. `provision_timeout_sec` covers queueing, image transfer and VM boot;
`boot_timeout_sec` covers subsequent guest bootstrap and readiness.

Use a Tart OCI image reference with a tag or SHA-256 digest. Tag resolution needs Docker
CLI with Buildx on the ALE Host (no Docker daemon required); use `docker login` there
for private registries. ALE queries manifest metadata without downloading disk layers.
Workers still need their own registry access to pull the resolved image. Digest-pinned
references do not require Host registry access or Buildx.

Provide root bootstrap access and Python 3.12+ at `python_path`, protected
from unprivileged writes. Make `python3` available through `/usr/local/bin` or
`/opt/homebrew/bin`; the four CLI Harnesses also need Node/npm at the
[required runtime version](../specs/autonomous-harness.md#shipped-integrations). Supply the Task account,
applications and licenses, with no preexisting model credentials. For desktop Tasks, either configure both Driver paths and preauthorize the service,
or opt into installation and UI authorization below.

Declare the image in `task.toml`:

```toml
schema_version = "1.4+ale.1"
artifacts = ["/Users/evaluator/output"]

[task]
name = "acme/macos-example"

[environment]
os = "macos"
vm_image = "registry.example.test/ale/macos:v1"
cpus = 2
memory_mb = 4096

[agent]
user = "evaluator"
network_mode = "public"
```

Replace the image reference, account and paths together. Without `[agent].sudo = true`, the
account must have no elevation rights. Orchard supports `network_mode = "public"` only. Use the
[standard Task layout](../specs/task-authoring.md) with `.sh` stage entries; the reward goes to
`/opt/ale/logs/verifier`, because the macOS root volume is read-only. Install stable
dependencies when preparing the image.

## Install and authorize Driver in each VM

For a local Mac controller/worker, use an otherwise compatible Tart base without
CuaDriver installed. No ALE base-image patch or pregranted Driver permissions are
required. The image still needs Python 3.12+, the logged-in admin account, root
bootstrap access and Screen Sharing. The UI prototype supports English/light mode
and US keyboard input; it is not a universal macOS permissions installer.

Install the optional Host dependencies (`ale-run[macos-ui]`; in this developer
workspace `uv sync` includes the test dependencies) and Xcode command-line tools.
Add to your private run TOML:

```toml
[vm.orchard.desktop_bootstrap]
password_env = "ALE_MACOS_ADMIN_PASSWORD"
retries = 3
```

Set `vm.orchard.boot_timeout_sec` to e.g. 600 seconds in the existing Orchard section.
Supply the named password via your credential mechanism. It is the logged-in Task
admin account's password, used for Screen Sharing and the Settings authorization
sheet; never put it in a TOML or command argument. Declare `[agent].sudo = true`.
Leave `driver_path` and `socket_path` unset: ALE selects its installed bundle and
socket. ALE downloads the Cua Driver release it pins for every platform, verifies the
archive checksum and installed app signature, and starts it in the Task account. The base
is unchanged.

Apple Vision OCR is compiled in a temporary Host directory. Permission UI screenshots
come only from the scoped VNC connection. Missing or all-black frames fail the attempt.
Screenshots and helper processes are scoped to provisioning. The normal desktop probe then verifies an
actual PNG from the Driver in the intended account. A UI grant is not enough.
ALE connects VNC before starting `cua-driver permissions grant` as the Task account.
The grant command runs concurrently with VNC handling of the System Settings grants
and the separate first-use direct-capture consent. After the command succeeds, ALE
restores its configured Driver service endpoint and verifies capture through that
endpoint. A pending grant is reused during UI retries; failure or cancellation tears
down the owned VM, including any permission-request processes still running there.
Failure stops provisioning and invokes owned-VM cleanup; no solver starts with
unverified permissions. Three retries means four total UI attempts, bounded by the
same boot deadline. Remote workers and arbitrary app authorization are unqualified.

`tests/fixtures/tasks/macos-desktop-smoke` is a standard ALE fixture for this path. Its
oracle uses Driver screenshot, a click, keyboard input and Save in TextEdit, and the verifier
checks the saved content. The click matters: a window in front does not hold the keyboard
until its document is clicked. The oracle is deterministic; it does not invoke an LLM.

## Run and validate

Run against a complete Task using your deployment configuration:

```bash
uv run ale lint /absolute/path/to/task
uv run ale prepare /absolute/path/to/task --config /path/to/run.toml
uv run ale run /absolute/path/to/task --agent oracle --config /path/to/run.toml
uv run ale run /absolute/path/to/task --agent codex-cli --config /path/to/run.toml
```

The Task's image reference, account and paths must match your deployment. `prepare`
resolves tags and records image identity; the oracle run creates a VM and exercises
setup, output collection and verification. Inspect the results and confirm VM cleanup.
Qualify desktop images with the intended visual agent and actual screenshot and input
operations.

For Provider conformance tests, set `ALE_TEST_ORCHARD=1`, `ALE_ORCHARD_TEST_CONFIG` to a
JSON `OrchardProviderConfig`, `ALE_ORCHARD_TEST_IMAGE` to a disposable image reference,
and `ALE_ORCHARD_TEST_USER` to its unprivileged account. Supply credentials through the
configured environment variables, then run
`uv run pytest tests/conformance/test_provider_orchard.py`. Include both Driver paths
for desktop tests. The suite creates fresh VMs and checks cleanup.

## Clean up

ALE tears down episode VMs. If cleanup fails, inspect the reported VM name and UID in
Orchard before reconciling it; do not delete a same-name replacement. Uncollected guest
logs and outputs can be lost when execution is interrupted.

After this local deployment's Tasks have finished, remove its services and state:

```bash
uv run scripts/setup_macos.py --cleanup
```

Cleanup refuses remaining VMs. Resolve them first; other deployments and services are
left unchanged.
