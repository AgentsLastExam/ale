# orbit-verify

Optional tools for Task-owned verification programs: Judges return dictionaries,
checks return ordinary data, and `write` atomically saves a final result.

```python
from orbit_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello world")
write({"reward": check["score"], "content": check})
```

`write` saves `{"reward": ...}` to Harbor's `/logs/verifier/reward.json`
(`C:\logs\verifier\reward.json` on Windows, `/opt/orbit/logs/verifier/reward.json` on macOS)
and the complete result to `verification.json` beside it. Only `reward` has framework
scoring semantics; other fields may be nested JSON. `write(result, feedback=text)` also
saves a feedback text, `feedback.md`, which a validation round of `validation/` shows the
agent.
Task code may write `reward.json` directly without this package. The program must exit
successfully for Orbit to accept it. Judge logs are independent.

The package imports neither `orbit.core` nor `orbit.run`. Orbit stages its pinned pure-Python
JSON Schema library alongside it. See [Verification](../../docs/specs/verification.md).

```bash
uv run pytest packages/orbit-verify/tests
```
