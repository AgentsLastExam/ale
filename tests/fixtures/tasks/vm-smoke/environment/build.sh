#!/bin/bash
# Where the agent writes its result, owned by the agent.
set -euo pipefail
mkdir -p "$ALE_HOME/output"
chown user:user "$ALE_HOME/output"
