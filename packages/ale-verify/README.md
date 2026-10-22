# ale-verify

Optional tools for Task-owned verification programs: Judges return dictionaries,
checks return ordinary data, and `write` atomically saves a final result.

```python
from ale_verify import checks, write

check = checks.text_equals("/home/user/output/result.txt", "hello world")
write({"overall": check["score"], "content": check})
```

Only `overall` has framework scoring semantics. Other fields may be nested JSON.
Task code may write the result directly to `ALE_VERDICT_PATH` without this package.
The program must exit successfully for ALE to accept it. Judge logs are independent.

The package imports neither `ale.core` nor `ale.run`. ALE stages its pinned pure-Python
schema validator alongside it. See [Verification](../../docs/specs/verification.md).

```bash
uv run pytest packages/ale-verify/tests
```
