# macOS Tasks

macOS Tasks run in fresh macOS VMs on the Mac that runs ALE, through a local Orchard
controller and Tart worker. A Task with `os = "macos"` goes there with no run settings.
Supported capabilities and cleanup behavior follow the
[sandbox contract](../specs/sandbox-image.md#runtime-behavior).

## Start the local Orchard

On Apple Silicon, run the setup script from the ALE checkout and leave it running:

```bash
uv run scripts/setup_macos.py
```

Then, in another shell:

```bash
uv run scripts/setup_macos.py --check
```

Setup pins Orchard **0.55.0** and Tart **2.41.1**, verifies release checksums, and starts
an isolated controller and worker. Their state, credentials and images live in
`~/.local/share/ale/macos`, where ALE finds them; existing services and system trust
settings are unchanged. The worker is a child of the setup command, which stays in the
foreground: macOS's Local Network privacy lets a program started from a Terminal or SSH
login reach the VMs only while it descends from that login, and a worker left behind
fails every VM with `no route to host`. Ctrl-C or closing the login stops the worker; run
setup again to restart it, including after a reboot. To reuse installed binaries at these
versions, pass `--orchard-path` and `--tart-path`. Setup does not bypass macOS signature
checks.

Setup also installs ALE's macOS framer, which frames images offline as root:
`framework_macos.sh` as `/usr/local/libexec/ale/framework-macos`, owned by root, and the
sudoers rule `/etc/sudoers.d/ale-framework-macos`, which lets the account that runs setup
run that one program as root without a password. sudo asks for the account's password the
first time and whenever the framer changes; ALE refuses to frame with an outdated copy.

## Choose an image

Name any Tart OCI image in `task.toml`, by tag or SHA-256 digest:

```toml
schema_version = "1.4+ale.1"
artifacts = ["/Users/admin/output"]

[task]
name = "acme/macos-example"

[environment]
os = "macos"
vm_image = "ghcr.io/cirruslabs/macos-tahoe-base:latest"
cpus = 2
memory_mb = 4096

[agent]
user = "admin"
network_mode = "public"
sudo = true
```

The first time a VM runs an image for an account, ALE frames it on this Mac: it clones the
image, installs the framework into the clone's disk without starting it (guestd, its own
Python, and Cua Driver with the Screen Recording and Accessibility grants macOS otherwise
takes only through System Settings), boots the clone once so macOS finishes the account's
first login, and keeps the result as a local image that every later VM clones. Framing
takes about two minutes and happens again only for a new image, account or framework.

The image needs:

- an `admin` account with the password `admin`, which Orchard logs in to every VM as;
  Cirrus Labs' Tart images have it. Framing turns on Remote Login and passwordless sudo
  for it;
- automatic login of the Task's account, which needs a home and its first login already
  done, so no Setup Assistant appears;
- FileVault off, so its disk can be framed;
- the applications and licenses the Task uses, with no preexisting model credentials.

`[agent].sudo` is applied at every boot: with `true` the account gets passwordless sudo,
with `false` ALE takes it out of the `admin` group and removes the sudoers rules that name
it. `admin` keeps passwordless sudo for ALE itself, so a Task that runs as `admin` must
declare `sudo = true`; a Task without it needs an image that signs in another account.

Tag resolution needs the Docker CLI with Buildx (no Docker daemon); use `docker login` for
private registries. Digest-pinned references need neither, and Tart pulls the image itself.

Only `network_mode = "public"` is supported. Use the
[standard Task layout](../specs/task-authoring.md) with `.sh` stage entries; the reward
goes to `/opt/ale/logs/verifier`, because the macOS root volume is read-only. Codex CLI,
Claude Code, Grok Build, OpenClaw CLI, computer-use, oracle and nop support macOS; the
four CLI Harnesses also need Node/npm at the
[required runtime version](../specs/autonomous-harness.md#shipped-integrations) in the
image. The `computer-use` Harness needs an Anthropic-compatible Gateway and a model
endpoint that accepts its computer tool schema. ALE appends the dialect route to
`gateway.base_url`: use the origin without a trailing `/v1` for an OpenAI-compatible endpoint.

`tests/fixtures/tasks/macos-desktop-smoke` exercises the desktop path: its oracle takes a
Driver screenshot, clicks, types and saves in TextEdit, and its verifier checks the file.
The click matters: a window in front does not hold the keyboard until its document is
clicked.

## Run and validate

```bash
uv run ale lint /absolute/path/to/task
uv run ale prepare /absolute/path/to/task
uv run ale run /absolute/path/to/task --agent oracle
```

`prepare` resolves tags and records image identity; the oracle run frames the image if
needed, creates a VM and exercises setup, output collection and verification.

For Provider conformance tests, set `ALE_TEST_ORCHARD=1`, `ALE_ORCHARD_TEST_IMAGE` to a
disposable image reference and `ALE_ORCHARD_TEST_USER` to the account it signs in, other
than `admin`, then run `uv run pytest tests/conformance/test_provider_orchard.py`. The suite creates fresh VMs
and checks cleanup.

## Clean up

ALE tears down episode VMs. If cleanup fails, inspect the reported VM name and UID in
Orchard before reconciling it; do not delete a same-name replacement. Uncollected guest
logs and outputs can be lost when execution is interrupted.

To remove the local services, their state, the framed images and the framer:

```bash
uv run scripts/setup_macos.py --cleanup
```

Cleanup refuses while VMs remain. Other deployments and services are left unchanged.
