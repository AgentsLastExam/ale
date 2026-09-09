# Task quality standard

A Task evaluates an agent's autonomous ability to produce a defined outcome or artifact by
interacting with a computer in a Sandbox. Its contract has three parts:

- **instruction** — the required outcome and any explicit constraints;
- **initial context** — the Sandbox state before the agent starts, including available data,
  software, services, permissions, and configured state;
- **verification** — the evaluation of the agent's final outcome.

Once the Task starts, the agent works without human clarification until it terminates. The
instruction and initial context must therefore define a feasible, sufficiently deterministic,
and unambiguous target. Multiple valid solutions are acceptable only when their common
acceptance boundary is clear.

This document owns Task design and acceptance standards. The folder and stage contracts are in
[Task authoring](task-authoring.md); scoring APIs, evidence inputs, and failure behavior are in
[Verification](verification.md).

## 1. Preserve the intended work

The Task must represent a coherent, realistic workflow and retain the substantive decisions and
difficulty of the intended capability. Its supplied inputs, scale, constraints, and initial state
must support that work. Construction conveniences must not turn the requested problem into a
trivial proxy, disclose the answer, or require unrelated expertise.

The initial context must admit at least one normal, solver-feasible route to completion within
the declared resources, permissions, and time. Blocked agent network access is the default; all
software, inputs, and local services required by that route must already be available. A runtime
download or network workaround is not a feasible blocked-network route. Intrinsic external
dependencies justify the narrowest practical allowlist. Open access is justified only when
restriction would materially change the intended capability, with that reason recorded in Task
metadata.

## 2. Keep the contract aligned

Instruction, initial context, and verification must describe the same outcome:

- every instructed requirement is feasible with the supplied context and is verified;
- verification scores no requirement, formatting detail, metadata, side effect, tool, or domain
  knowledge that the instruction does not require;
- verification runs correctly with its supplied code, data, dependencies, and permissions;
- required inputs, software, services, and initial state exist before the autonomous agent phase;
- scoring is faithful to the requested outcome rather than an unstated implementation path.

If the instruction does not constrain commands, tools, or intermediate steps, every valid way to
produce the outcome must be accepted. If a path or behavior is an explicit part of
the requested outcome, verification must check it. State exact output locations, formats,
tolerances, and protected side effects whenever correctness depends on them.

Incidental delivery conventions should be explicit when this removes acceptance ambiguity without
reducing difficulty, realism, or substantive decisions. Meaningful valid outcomes must not be
narrowed, and solution steps must not be prescribed merely to simplify verification. Every
retained degree of freedom needs a clear acceptance boundary that the chosen verification method
can assess fairly. The public instruction defines that boundary before the evaluated agent works;
the verifier and oracle cannot create additional hidden requirements.

## 3. Evaluate evidence fairly

Expected facts and acceptance conditions must follow from the supplied inputs and the public
contract. A particular solver output or oracle implementation is not an independent source of
truth. An oracle receiving full credit establishes one accepted outcome, not the verifier's
coverage or fairness.

Equivalent expressions of correct content must receive equivalent credit. Superficially similar
but materially incorrect content must be distinguished from correct content. Deterministic checks
are suitable when parsing and normalization can reliably preserve these distinctions across the
permitted variations. Semantic or perceptual requirements need an LLM Judge or Agent Judge when
deterministic inspection cannot establish them reliably; the methods may be combined. The chosen
evidence must expose the actual property being assessed. Text extraction alone does not establish
visual correctness, and a proxy's presence does not establish the outcome it is meant to measure.

Rubrics and decision rules must assess the instruction's level of precision. Scores must provide
meaningful detail about the required outcomes, and full credit must cover all material
requirements. Empty, placeholder, malformed, stale, incomplete, or otherwise materially incorrect
outcomes must not receive credit for requirements they fail. Verification failures are not evidence
that the evaluated agent failed the Task; their technical behavior belongs to the verification
contract.

## 4. Resist reward hacking

Full credit must require evidence of the intended capability, not knowledge of the verifier or a
cheap proxy for success. Protect reference data and scoring internals from the evaluated agent,
and exclude initial-context information that reveals or makes it trivial to derive the
answer. Verification must reject self-reported success, incomplete artifacts, superficial proxy
outputs, and other shortcuts that bypass the work the instruction is intended to measure.

A Task is ready only when no unresolved mismatch or exploitable shortcut could materially change
which valid outcomes receive credit or which invalid outcomes are accepted.
