#!/usr/bin/env bash
# The oracle proves only that the verifier awards full credit; delegation is checked
# from the trajectory by live acceptance.
set -euo pipefail

python3 - <<'PY'
import json
import os

with open(os.environ["ALE_PARAMS_JSON"], encoding="utf-8") as handle:
    marker = json.load(handle)["marker"]
with open("/home/user/output/child.txt", "w", encoding="utf-8") as handle:
    handle.write(marker)
PY
