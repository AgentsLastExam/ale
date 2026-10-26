import json
import os
import re

from ale_verify import checks, write

with open(os.environ["ALE_PARAMS_JSON"], encoding="utf-8") as handle:
    marker = json.load(handle)["marker"]
# A trailing newline from a shell write is not a different answer.
child = checks.text_regex("/home/user/output/child.txt", rf"\A{re.escape(marker)}\s*\Z")
write({"overall": child["score"], "child": child})
