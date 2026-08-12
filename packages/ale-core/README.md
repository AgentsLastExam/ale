# ale-core

`ale-core` owns the stable contracts shared by ALE implementations. It contains strict
models for Tasks, sandboxes, harnesses, traces, results and provenance, plus the abstract
interfaces implemented by `ale-run`.

It never imports the orchestrator or verification implementation. A change here is a
contract change when it alters a public model, persisted record or implementation
interface; update the owning specification under [`../../docs/specs`](../../docs/specs)
in the same change.

Important modules:

| Module | Owns |
|---|---|
| `taskspec.py` | authored and effective Task schemas |
| `sandbox.py` | Provider and Sandbox contracts |
| `harness.py` | Harness families, sessions and native continuation |
| `environment.py` | Episode lifecycle contracts and context |
| `trace.py`, `trajectory.py` | execution evidence and ATIF trajectory |
| `result.py`, `lock.py` | terminal result and reproducibility provenance |

Run its tests from the repository root with `just test` and verify dependency direction
with `just lint`.
