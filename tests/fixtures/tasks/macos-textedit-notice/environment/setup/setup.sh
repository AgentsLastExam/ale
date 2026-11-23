#!/usr/bin/env bash
set -euo pipefail
test "$ORBIT_HOME" = /Users/admin
mkdir -p /Users/admin/output
rm -f /Users/admin/output/notice.rtf
chown admin:staff /Users/admin/output
