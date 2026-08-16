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

Verification may combine deterministic code, LLM Judges, and Agent Judges. Whatever method is
used, it must evaluate performance fairly and produce explicit, fine-grained scores in the
inclusive range from `0` to `1`.

## 1. Keep the contract aligned

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

Design for blocked agent network access by default. Bake or stage every required dependency,
input, and local service into the initial context. Use the narrowest practical allowlist when
external access is intrinsic to the Task. Use open network access only when restricting it would
materially change the intended capability, and record that justification in the Task metadata.

## 2. Resist reward hacking

Full credit must require evidence of the intended capability, not knowledge of the verifier or a
cheap proxy for success. Protect reference data and scoring internals from the evaluated agent,
and inspect the initial context for information that reveals or makes it trivial to derive the
answer. Verification must reject self-reported success, incomplete artifacts, superficial proxy
outputs, and other shortcuts that bypass the work the instruction is intended to measure.

A Task is ready only when no unresolved mismatch or exploitable shortcut could materially change
which valid outcomes receive credit or which invalid outcomes are accepted.
