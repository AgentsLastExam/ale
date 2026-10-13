import json
import os

from ale_verify import Verification, checks

with open(os.environ["ALE_PARAMS_JSON"], encoding="utf-8") as handle:
    expected = json.load(handle)["greeting"] + "\n"

verification = Verification()
verification.check(
    "content",
    checks.text_equals("/home/user/output/result.txt", expected),
)
verification.write()
