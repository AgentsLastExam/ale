# Run episodes on Daytona

The Daytona backend runs Linux container Tasks on [Daytona](https://www.daytona.io)'s cloud
instead of the local Docker daemon. Use it when the Host cannot run enough sandboxes at
once. Docker stays on the Host: it builds and checks each Task image, and the Provider
pushes the result to Daytona. VMs and Windows stay with the QEMU backend. How the backend
holds ALE's guarantees on Daytona is in
[Providers](../../packages/ale-run/src/ale/run/providers/README.md#daytona).

## Before the first run

- **A Daytona organization on Tier 3 or Tier 4.** ALE switches a sandbox's network on and
  off while it runs, which Daytona allows only at those tiers. On a lower tier the first
  `open_egress` fails with a message saying so.
- **An API key** from the Daytona dashboard, in the checkout's `.env`:

  ```bash
  DAYTONA_API_KEY=<your key>
  ```

  The key stays on the Host. To use another variable, set
  `container.daytona.api_key_env` to its name.
- **`docker` and `ssh` on the Host.** `ssh` is the OpenSSH client: guestd and the Gateway
  tunnel run over it. Daytona's gateway host key is recorded in
  `~/.cache/ale/daytona/known_hosts` on first use; delete that file if Daytona rotates it.

## Select the backend

In a run TOML, or with `--set` on the command line:

```toml
[container]
provider = "daytona"

[container.daytona]
target = "us"                # Daytona region
lifetime_hours = 24          # Daytona deletes a sandbox this long after creation
retained_lifetime_hours = 72 # the same, for sandboxes kept with sandbox_retention
gpu_types = ["H100"]         # GPU models in order of preference; [] lets Daytona choose
```

```bash
uv run ale run TASK --agent claude-code --set container.provider=daytona
```

`container.gpus` selects Docker devices and is rejected with Daytona.

## GPU Tasks

A Task that sets `resources.gpus` gets that many GPUs of the first model in `gpu_types`
with capacity, in Daytona's GPU region, which Daytona chooses itself. The organization
needs GPU quota there; Daytona's usage report lists it per region, with the CPU, memory
and disk each GPU may come with. A sandbox takes up to 8 GPUs, and only NVIDIA models can
be recorded, so `MI355X` cannot be used.

GPU sandboxes are billed per GPU while they exist, and a kept one runs until
`retained_lifetime_hours`. The first GPU episode of an image also waits for Daytona to pull
it into the GPU region, about a minute and a half for the desktop base.

## Allowlisted Tasks

A Task with `network.mode: allowlist` downloads straight from Daytona to its declared
hosts, never through your machine. Daytona takes domain names only, at most 100, so a Task
whose allowlist names an address, a port or a pattern is refused on Daytona.

## Checking an organization

To check that an organization gives every guarantee, run the Daytona suites with the API
key in the environment. `ALE_TEST_DAYTONA_GPU=1` runs them on GPU sandboxes as well, which
costs a few GPU-minutes:

```bash
ALE_TEST_DAYTONA_GPU=1 uv run pytest -m needs_daytona tests/conformance/test_provider_daytona.py tests/integration/test_daytona_episode.py
```

## What it creates in your organization

- **Sandboxes**, labeled `ale.run.managed=true` with the episode, role and retention. They are
  deleted when the episode ends, and by Daytona after `lifetime_hours` if a run dies first.
  A kept sandbox is listed by `ale sandbox list` and removed by `ale sandbox destroy
  daytona:<id>`; both touch only sandboxes carrying the `ale.run.*` labels, so other tools
  in a shared organization, including ones that label their own sandboxes `ale.*`, are not
  affected.
- **One registry entry**, `ale-transient`, which ALE keeps current so Daytona can build from
  the images it pushes; leave it in place. Daytona removes pushed images on its own
  schedule, and ALE pushes again when one is gone.

Nothing else is created: no snapshots, so Daytona's snapshot quota is not used.

## Limits to know

- **Resources are whole GiB.** A Task with `memory_mb: 1536` is refused rather than rounded.
  Without `storage_mb`, Daytona gives its default disk: 3 GiB, or 512 GiB with a GPU.
- **Quotas.** A create Daytona rate-limits waits for up to five minutes; a quota or
  capacity refusal fails the episode with Daytona's message.
- **Cold start.** The first episode of an image pushes it to Daytona, which is minutes for
  a large desktop base. Later sandboxes start in about 15 seconds.
- **The SSH session is the sandbox's lifeline.** If it drops, the tunnel reconnects but an
  operation in flight fails, as it would if a `docker exec` session died.
- **Daytona sees the sandbox.** The image and everything run in it are on Daytona's
  infrastructure.

## When something fails

| Message | Cause |
|---|---|
| `Daytona rejected the API key` | `DAYTONA_API_KEY` is wrong or for another organization |
| `would not change the sandbox's network settings at runtime` | the organization is below Tier 3 |
| `is not a whole number of GiB` | the Task's memory or storage is not a multiple of 1024 MB |
| `Daytona's allowlist takes domain names` | the Task's allowlist names an address, a port or a pattern |
| `can still reach Daytona's daemon` | the sandbox could not be sealed; it was destroyed. Report it with the image |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | delete `~/.cache/ale/daytona/known_hosts` |
