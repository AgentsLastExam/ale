# ADR 0014: Autonomous harness configuration and resources

## Status

Accepted.

## Context

Autonomous harnesses own their interaction loop, but ALE still owns the conditions that
make runs comparable: configuration, declared resources, model routing, limits, traces,
and provenance. The first Claude Code adapter accepted an open-ended `kwargs` mapping,
silently ignored unknown keys, injected a screen-control MCP server into every run, and
had no native continuation contract.

## Decision

- Every shipped autonomous harness has one complete, version-controlled TOML preset.
- Harness-specific settings are strictly validated by that harness; unknown or
  unsupported values fail before sandbox provisioning.
- `default` means omit the native option. `unlimited` is an explicit no-cap value.
- Task, preset, Run, and CLI Skill/MCP declarations form an additive union. Only declared
  resources enter the sandbox.
- ALE's MCP contract supports stdio and Streamable HTTP. Vendor files are adapter output,
  not core input.
- Screen control is the ordinary opt-in MCP server `cua-desktop`.
- Gateway model-call, token, and cost ceilings remain authoritative. Harness-native
  controls are separate and recorded separately.
- Native continuation is optional, exact-session, and limited to the original live
  sandbox. It sends only new input.
- The Gateway writes transport truth; harnesses parse collected native logs into a
  canonical ATIF v1.7 trajectory on the host.
- Effective settings, limits, resources, and continuation conditions are recorded in
  RunLock.

No central harness capability registry is introduced. Each adapter either implements a
configured feature or rejects it explicitly.

## Consequences

Adding a harness requires a strict settings model, complete preset, declared resource
translation, episode isolation, Gateway routing, evidence parser, provenance, and common
conformance coverage. Remote Skills, authenticated remote MCP, modality negotiation, and
cross-sandbox continuation remain separate future features.
