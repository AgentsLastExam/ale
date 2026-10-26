#!/usr/bin/env bash
set -euo pipefail
# Reference final state for outcome-verifier validation, not a desktop rollout.
test "$ALE_HOME" = /Users/admin
cat > /Users/admin/output/notice.rtf <<'RTF'
{\rtf1\ansi\deff0{\fonttbl{\f0 Helvetica;}}
\pard\qc\f0\fs48\b Community Design Meetup\b0\par
\pard\ql\fs24 Date: 12 November 2026\par
Time: 15:30-17:00\par
Location: Harbour Studio, Room 2\par
Agenda\par
\u8226? Welcome and introductions\par
\u8226? Poster design demo\par
\u8226? Questions and next steps\par
Contact: Maya Chen | hello@example.org\par
}
RTF
