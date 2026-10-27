# macOS Tasks through Orchard

Use an Orchard controller and ARM64 Tart worker to run each Task in a fresh macOS VM.
Supply an image meeting the [macOS image requirements](../specs/sandbox-image.md#what-a-macos-image-needs).
Supported capabilities and cleanup behavior follow the
[sandbox contract](../specs/sandbox-image.md#runtime-behavior).

## Install and start locally

On Apple Silicon, run the setup script from the ALE checkout:

```bash
uv run scripts/setup_macos.py
uv run scripts/setup_macos.py --check
source "$HOME/.local/share/ale/macos/credentials.env"
```

Setup pins Orchard **0.55.0** and Tart **2.41.1**, verifies release checksums, and starts
an isolated controller and worker with private state and TLS configuration. Existing
services and system trust settings are unchanged. To reuse installed binaries at these
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
route and preserves the Gateway's allocated port. No Gateway setting is needed at setup.
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
applications and licenses, with no preexisting model credentials. For desktop Tasks,
configure both Driver paths and preauthorize the service.

Declare the image in `task.yaml`:

```yaml
spec_type: core/v1
name: macos-example
os: macos
image:
  kind: vm
  ref: registry.example.test/ale/macos:v1
  user: evaluator
resources:
  cpus: 2
  memory_mb: 4096
  sudo: false
network:
  mode: open
artifacts:
  - /Users/evaluator/output
```

Replace the image reference, account and paths together. For `sudo: false`, the account
must have no elevation rights. Use the [standard Task layout](../specs/task-authoring.md) with
POSIX `run.sh` stage entries; install stable dependencies when preparing the image.

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
