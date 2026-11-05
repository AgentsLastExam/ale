#!/bin/sh
set -eu
mkdir -p /Users/tester/output
{
    printf 'user=%s\n' "$(id -un)"
    if dseditgroup -o checkmember -m "$(id -un)" admin >/dev/null 2>&1; then
        echo admin=yes
    else
        echo admin=no
    fi
    if sudo -n true 2>/dev/null; then echo sudo=allowed; else echo sudo=denied; fi
    printf 'build=%s\n' "$(cat /Users/tester/from-build.txt 2>/dev/null || echo missing)"
} > /Users/tester/output/result.txt
