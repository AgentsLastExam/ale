# 0013 — Two roots in a sandbox, and nobody changes an owner

**Status:** accepted

## Context

A sandbox had one framework root, `/ale`, holding two kinds of thing with opposite
requirements. The setup and verify stages and the rewards file live there and the agent
must never read them — the verify stage holds the scorer. Task data lived there too, at
`/ale/input` and `/ale/output`, and the agent must be able to write it.

Keeping both working needed a chown on every declared path: the framework created them as
root and then handed each one over. That worked, and it hid two problems. The engine was
changing ownership, which an earlier decision had said it would not do — the task's setup
was supposed to decide what it opened to the agent. And a task naming a path outside the
agent's reach was silently made to work, so nothing ever caught it; on an image whose
agent account was named differently, it would fail somewhere far from the cause.

There was also a second answer to a question the image had already answered. The image
declares its agent account; the run separately configured a `work_dir`. Two facts
describing the same thing can disagree, and the engine cannot tell which is right.

## Decision

**A sandbox has exactly two roots.**

`/opt/ale` is the framework's: root-owned, mode 700, holding the guest service, the setup
and verify stages, and the rewards file. The scorer is now out of reach by permission
rather than by timing — it used to be kept away only by being uploaded late.

`/home/<user>` is the agent's, and everything the agent touches is under it: the task's
declared destinations, its artifacts, the harness's files. It is derived from `ale.user`,
not declared or configured separately.

**The framework creates declared paths as the agent, and never changes an owner.** One
step instead of two. It also turns the convention into a mechanism: a task declaring a
path the agent cannot create fails immediately, naming the home.

The oracle is the exception that proves the rule. It stands in for the agent and runs as
the agent, so it is staged in the home rather than with the framework's machinery, where
the account allowed to run it could not read it. Under a real agent it is never uploaded.

## Consequences

Tasks write absolute paths under `/home/<user>`. No placeholder syntax exists, and none is
needed: a task already names its image, so it already knows the account that image
declares.

A task that wants a path elsewhere — a simulation domain at `/opt/sim/scenes`, say — must
create it in its own setup, which runs as the framework. That is the task's decision and
the task's problem, which is where it belonged.

`work_dir`, `/ale/kits` and `ALE_HOME` are gone. The first was the duplicate answer; the
other two had been dead since kits moved to the interpreter's own `site-packages`.
