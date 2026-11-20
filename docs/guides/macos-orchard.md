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

Setup pins Orchard **0.58.0**, Tart **2.41.1** and Softnet **0.25.0**, verifies release
checksums, and starts an isolated controller and worker. Once the pinned releases are in
place it stops services an earlier pin started, and refuses while their controller holds VMs
or their worker runs without its controller. Their state, credentials and images live in
`~/.local/share/ale/macos`, where ALE finds them; existing services and system trust
settings are unchanged. The worker is a child of the setup command, which stays in the
foreground: macOS's Local Network privacy lets a program started from a Terminal or SSH
login reach the VMs only while it descends from that login, and a worker left behind fails
every VM with `no route to host`. Ctrl-C or closing the login stops the worker; run setup
again to restart it, including after a reboot. To reuse installed binaries at these
versions, pass `--orchard-path` and `--tart-path`. Setup does not bypass macOS signature
checks.

Setup also installs ALE's macOS framer, which frames images offline as root:
`framework_macos.sh` as `/usr/local/libexec/ale/framework-macos`, owned by root, and the
sudoers rule `/etc/sudoers.d/ale-framework-macos`, which lets the account that runs setup
run that one program as root without a password. sudo asks for the account's password the
first time and whenever the framer changes; ALE refuses to frame with an outdated copy.

On macOS 26 or later, setup also installs Softnet, the packet filter that seals the VMs of
`no-network`, `allowlist` and `blocklist` Tasks on this Mac, as
`/usr/local/libexec/ale/softnet`, owned by root and set-user-ID because it sets a VM's network
up as root, and puts it on the worker's PATH; sudo asks for the password for that too.
`--check` says whether the worker has it. Without it, as on macOS before 26, only `public` Tasks run. Softnet sets the DHCP
lease time for VM networks to 10 minutes, for every VM and Internet Sharing on the Mac, and
that setting stays after cleanup
([Providers](../../packages/ale-run/src/ale/run/providers/README.md#restricted-networking)).

## Choose an image

Name any Tart OCI image in `task.toml` as `[environment].image`, by SHA-256 digest or by a
tag that does not move (`ale lint` reports `latest`):

```toml
schema_version = "0.2.0"
artifacts = ["/Users/admin/output"]

[task]
name = "acme/macos-example"

[environment]
os = "macos"
image = "ghcr.io/cirruslabs/macos-tahoe-base@sha256:4aaa751fa7538902d73c767f27a066fc87e83f183069da4e4b5022863abd4c77"
cpus = 2
memory_mb = 4096

[agent]
user = "admin"
network_mode = "no-network"
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
  To install them on a stock image instead, name it the same way and add
  `environment/build.sh`, which ALE runs once as root in a VM of the base framed for
  `admin`, so the base must sign `admin` in, and keeps; the result is framed for the
  Task's account when a sandbox first uses it, so the build may create that account
  ([Task authoring](../specs/task-authoring.md#environment)).

`[agent].sudo` is applied at every boot, as on Linux: a last sudoers rule gives the account
passwordless sudo, or with `false` denies it every command, and ALE also takes it out of the
`admin` group. `admin` keeps passwordless sudo for ALE itself, so a Task that runs as
`admin` must declare `sudo = true`; for a Task without it, create and sign in another
account in `build.sh`: add it with `sysadminctl` and its home at `$ALE_HOME`, set
`autoLoginUser` in `/Library/Preferences/com.apple.loginwindow` and its password in
`/etc/kcpassword`, and mark the account's Setup Assistant panes seen.

Tag resolution needs the Docker CLI with Buildx (no Docker daemon); use `docker login` for
private registries. Digest-pinned references need neither, and Tart pulls the image itself.

Every `network_mode` is supported; the sealed modes need Softnet, and their setup and
verification reach IPv4 addresses only. Use the
[standard Task layout](../specs/task-authoring.md) with `.sh` stage entries; the reward
goes to `/opt/ale/logs/verifier`, because the macOS root volume is read-only. Every
shipped Harness supports macOS. The npm-installed CLIs need Node/npm at the
[required runtime version](../specs/autonomous-harness.md#shipped-integrations) in the
image, and Hermes and OpenHands CLI need Git, which the ALE bases provide on Linux and
Windows. The `computer-use` Harness needs an Anthropic-compatible Gateway and a model
endpoint that accepts its computer tool schema.

`tests/fixtures/tasks/macos-desktop-smoke` exercises the desktop path: its oracle takes a
Driver screenshot, clicks, types and saves in TextEdit, and its verifier checks the file.
The click matters: a window in front does not hold the keyboard until its document is
clicked. `tests/fixtures/tasks/macos-unprivileged` builds an administrator with passwordless
sudo on the base, with its home and a file in it at `ALE_HOME`, and signs it in; with
`sudo = false`, its oracle finds the account out of the `admin` group, without sudo, and the
file its build left.

## Run and check

```bash
uv run ale lint /absolute/path/to/task
uv run ale prepare /absolute/path/to/task
uv run ale run /absolute/path/to/task --agent oracle
```

`prepare` resolves tags, records image identity and, for a Task with `environment/build.sh`,
builds its image; the oracle run frames the image if needed, creates a VM and exercises
setup, output collection and verification.

For Provider conformance tests, run
`ALE_TEST_ORCHARD=1 uv run pytest tests/conformance/test_provider_orchard.py`. The suite
builds `macos-unprivileged` on this Mac the first time, so its sudo=false cases run as an
account other than `admin`, then creates fresh VMs of it, sealed as `no-network` unless a
case asks otherwise, and checks cleanup. The CLI
Harness integration tests run in macOS VMs as well ([Tests](../../tests/README.md)).

## Clean up

ALE tears down episode VMs. Kept VMs and snapshots are listed by `ale sandbox list` and
stay until `ale sandbox destroy <handle>`; until then, `scripts/setup_macos.py --cleanup`
refuses. If a teardown fails, inspect the reported VM name and UID in Orchard before
reconciling it; do not delete a same-name replacement. Uncollected guest logs and outputs
can be lost when execution is interrupted.

To remove the local services, their state, the framed images, the framer and Softnet:

```bash
uv run scripts/setup_macos.py --cleanup
```

Cleanup refuses while VMs remain. Other deployments and services are left unchanged.
