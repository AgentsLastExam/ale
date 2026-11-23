#!/bin/bash
# Where the agent writes its result, owned by the agent.
set -euo pipefail
mkdir -p "$ORBIT_HOME/output"
chown user:user "$ORBIT_HOME/output"
