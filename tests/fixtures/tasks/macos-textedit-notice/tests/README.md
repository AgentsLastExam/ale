# TextEdit notice: desktop integration draft

A twenty-minute desktop Task for the same input under `computer-use` and `codex-cli`.
The only deliverable is `notice.rtf`; TextEdit ships with macOS. The public base is
pinned to the Tahoe image inspected on 2026-10-27. It signs only its `admin` account in,
which ALE also controls the VM through, so the Task declares `sudo = true` even though
editing the document needs no elevation.

## Run prerequisites

ALE installs Cua Driver and its privacy grants when it first frames the image; see the
[macOS guide](../../../../../docs/guides/macos-orchard.md). For
`codex-cli`, enable the Run-level `cua-desktop` MCP; `computer-use` uses the Sandbox desktop
API directly. Keep the Task input identical between both runs.

## Verification

The verifier reads actual saved RTF attributes through macOS AppKit. Nine equal-weight
checks cover readable RTF, title, event details, updated time, agenda, contact, content
order, title formatting and body formatting. Whitespace and blank lines are tolerated.
The oracle supplies a reference RTF to validate these outcome checks.

Run `uv run ale lint tests/fixtures/tasks/macos-textedit-notice` for structural checks.
Full validation and model runs require the local Orchard from the guide.
