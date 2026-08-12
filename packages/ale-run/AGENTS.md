# ale-run agent guide

Read [`README.md`](README.md) before changing the runtime. The normal ownership path is:

```text
CLI composition → Task/image preparation → Episode administration
→ StandardEnvironment phases → Provider/Sandbox + Harness/Gateway → records
```

- `cli/` parses and composes; it does not implement Provider, Harness or phase behavior.
- `episode.py` owns one episode's session, leases, recording, failure mapping and result.
- `environments/` owns phase ordering and phase-local state.
- `providers/` implements infrastructure; `harnesses/` uses only the Sandbox contract.
- `gateway/` never imports Providers or Environments.
- `guestd/` is standard-library only and must remain deployable as copied source.
- Prefer explicit typed runtime state over `dict[str, object]`, `Any`, `getattr` or string
  keys shared between modules.
- Keep provider differences local. Extract shared code only when semantics, failure
  behavior and tests are genuinely identical.

Run `just lint && just test`; add the smallest relevant integration selection for any
Sandbox, network, episode or verification change.
