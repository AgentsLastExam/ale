# TextEdit notice: desktop integration draft

A twenty-minute desktop Task for the same input under `computer-use` and `codex-cli`.
The only deliverable is `notice.rtf`; TextEdit ships with macOS. The public base is
pinned to the Tahoe image inspected on 2026-10-27. Its default admin account requires
`sudo: true` even though editing the document needs no elevation.

## Run prerequisites

The image alone does not supply ALE's authorized desktop service: use `desktop_bootstrap`
from the [macOS guide](../../../../../docs/guides/macos-orchard.md), or an image with its own
authorized Driver. For `codex-cli`, enable the Run-level `cua-desktop` MCP; `computer-use` uses
the Sandbox desktop API directly. Keep the Task input identical between both runs.

## Verification

The verifier reads actual saved RTF attributes through macOS AppKit. Nine equal-weight
checks cover readable RTF, title, event details, updated time, agenda, contact, content
order, title formatting and body formatting. Whitespace and blank lines are tolerated.
The oracle supplies a reference RTF to validate these outcome checks.

Run `uv run ale lint tests/fixtures/tasks/macos-textedit-notice` for structural checks. Full validation
and model runs require the desktop-ready deployment above.
