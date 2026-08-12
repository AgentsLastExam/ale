# Tests

| Directory | Purpose | Infrastructure |
|---|---|---|
| `unit/` | fast in-process behavior | none |
| `conformance/` | reusable public-contract suites | usually none |
| `integration/` | real Sandbox and cross-component behavior | marked by backend |
| `acceptance/` | live provider, model, GPU or GUI qualification | explicit live markers |

```bash
just test       # unit and lightweight conformance
just test-int   # conformance and integration
just lint       # formatting, lint and import boundaries
```

Tests that require infrastructure must carry the matching marker (`needs_docker`,
`needs_kvm`, `needs_gui`, `needs_llm`, or a Hugging Face marker) so local and CI runs can
select them deliberately. Prefer testing the public contract; use private helpers only
when the helper itself is the behavior under test.
