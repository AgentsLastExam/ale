# Tests

| Directory | Purpose | Infrastructure |
|---|---|---|
| `unit/` | fast in-process behavior | none |
| `conformance/` | reusable public-contract suites | usually none |
| `integration/` | real Sandbox and cross-component behavior | marked by backend |
| `acceptance/` | live provider, model, GPU or GUI qualification | explicit live markers |

```bash
just test             # unit and lightweight conformance
just test-int         # conformance and integration, without VMs and agent CLI installs
just test-harnesses   # every pinned agent CLI, really installed (needs_cli_install)
just test-vm          # VM Sandboxes on KVM (needs_kvm)
just lint             # formatting, lint and import boundaries
```

`just test-int` and CI's container backend run four test files at a time
(`pytest -n 4 --dist loadfile`), so an integration test neither relies on what another file
leaves behind nor assumes it is the only run on the host. Every runc Docker sandbox whose
network is not `open` gets a network of its own, and Docker's default address pools hold
about 31; a host that runs several suites at once configures smaller
`default-address-pools`. The Kata tests run only where Kata is installed
([Providers](../packages/ale-run/src/ale/run/providers/README.md#docker-on-kata-containers)).

Run `just test-harnesses` after changing a Harness, a Gateway dialect or a CLI pin, and
`just test-vm` after changing VM or Windows behavior. CI's backend jobs run all three. After
changing macOS behavior, run the Orchard suites on a Mac
([macOS guide](../docs/guides/macos-orchard.md)). On a Mac whose local Orchard
`scripts/setup_macos.py` keeps running, `ALE_TEST_CLI_OS=macos just test-harnesses` installs
and runs every CLI in a macOS VM instead of a container: one of an image of the pinned Tahoe
base with Node.js, built once.

Tests that require infrastructure must carry the matching marker (`needs_docker`,
`needs_kvm`, `needs_orchard`, `needs_gui`, `needs_llm`, `needs_cli_install`, or a Hugging
Face marker) so local and CI runs can select them deliberately. Prefer testing the public
contract; use private helpers only when the helper itself is the behavior under test.

Live acceptance tests make billed model calls, so CI runs none of them: a change to what
one covers runs it locally before merging. Most run only with
`ALE_RUN_LIVE_ACCEPTANCE=1`; the Judge tests instead skip without their provider's key.
They load the checkout's `.env` before any test module is imported, and `ALE_ENV_FILE`
points every worktree at one file. Every agent and Judge call resolves its key and endpoint
exactly as `ale` does, from `ALE_<PROVIDER>_API_KEY` and `ALE_<PROVIDER>_BASE_URL`, so a
shared gateway is configured once in `.env` rather than per test. Only the models are test
knobs: `ALE_LIVE_ANTHROPIC_MODEL` (default `claude-sonnet-5`) and `ALE_LIVE_OPENAI_MODEL`
(default `gpt-5.6-luna`); xAI calls use `grok-4.5`.
