#!/usr/bin/env bash
# ALE does not create declared artifacts, and setup runs as the framework, so the output
# directory is made here and handed to the agent account.
set -euo pipefail

install -d -o user -g user /home/user/output
rm -f /home/user/output/child.txt
