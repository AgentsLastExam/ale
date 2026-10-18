# ale-verify

`ale-verify` is the dependency-free Python library staged into the active verification
Sandbox. Task authors use it to combine deterministic checks, optional LLM or agent
Judges, metrics and aggregate rewards into one atomic verification record.

```python
from ale_verify import Verification, checks

verification = Verification()
verification.check(
    "reward",
    checks.text_equals("/home/user/output/result.txt", "hello world"),
)
verification.write()
```

`write()` adds the `overall` reward as the weighted mean of every criterion unless the
verifier defined its own `overall` aggregate or check. A stat cannot be named `overall`.

Verification code runs from the Task's `verify/` directory. It writes
`verification.json` and the final reward envelope through paths supplied by ALE; callers
do not choose those framework paths.

The package intentionally imports neither `ale.core` nor `ale.run`, so it can be copied
into a Task image without pulling in the engine. Its public API is documented in
[`../../docs/specs/verification.md`](../../docs/specs/verification.md).

Judge `files` can include text, images, and PDFs. LLM Judge sends supported media as native
multimodal input; Agent Judge inspects files through its harness. Both receive inline
`reference` text. See the verification specification for evidence formats, limits, and
failure behavior.

For an LLM response whose multiple fields a Task scores deterministically, use
`verification.judge_json(...)` with a function that returns a `CheckResult`.

```bash
uv run pytest packages/ale-verify/tests
```
