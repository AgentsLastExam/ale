# ale-verify

Optional tools for Task-owned verification programs: Judges return dictionaries,
checks return ordinary data, and `write` atomically saves a final result.

```python
from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello world")
write({"reward": check["score"], "content": check})
```

`write` saves `{"reward": ...}` to Harbor's `/logs/verifier/reward.json`
(`C:\logs\verifier\reward.json` on Windows) and the complete result to `verification.json`
beside it. Only `reward` has framework scoring semantics; other fields may be nested JSON.
Task code may write `reward.json` directly without this package. The program must exit
successfully for ALE to accept it. Judge logs are independent.

The package imports neither `ale.core` nor `ale.run`. ALE stages its pinned pure-Python
schema validator alongside it. See [Verification](../../docs/specs/verification.md).

```bash
uv run pytest packages/ale-verify/tests
```
