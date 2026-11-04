#!/bin/sh
set -eu
mkdir -p /Users/admin/output
: > /Users/admin/output/result.txt
chown -R admin /Users/admin/output
sudo -u admin open -a TextEdit /Users/admin/output/result.txt
sleep 2
