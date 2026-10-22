import json
import os

from ale_verify import checks, write

with open(os.environ["ALE_PARAMS_JSON"], encoding="utf-8") as handle:
    expected = json.load(handle)["greeting"] + "\n"
check = checks.text_equals("/home/user/output/result.txt", expected)
write({"overall": check["score"], "content": check})
