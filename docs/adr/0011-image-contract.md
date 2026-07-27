# 0011 — An image declares what it provides

**Status**: accepted (2026-07-26)

## Context

The engine inferred three things about an image: whether to substitute a command that
keeps the sandbox alive, whether to wait for a graphical session, and which interpreter to
run the guest service on. Each inference was wrong at least once, and each failure was
silent in a different way.

Substituting a keep-alive command replaced whatever the image started for itself. For the
desktop image that was the entire graphical session, so the image claimed a desktop and
had none — every screenshot failed with "unable to open display", which reads as a broken
sandbox rather than a replaced command.

Treating a sandbox as ready once the guest service answered meant using a screen that did
not exist yet; the service starts instantly and a desktop takes twenty seconds.

Choosing an interpreter via a per-image hint went wrong at the other end: the hint was a
symlink to a venv interpreter, and Python derives its prefix from the invocation path, so
the symlinked copy silently lost the venv's packages and the fast screenshot path fell
back for as long as the mechanism existed.

## Decision

**An image declares what it provides; the engine reads those declarations.** It never
infers behaviour from a name, and never probes by attempting an operation to see whether
it works — a failure caused by a slow service is indistinguishable from one caused by a
missing capability.

`docs/specs/sandbox-image.md` is the contract. An image must provide one system
interpreter with the guest service's dependencies already installed, an unprivileged
account with a real home, a command that keeps the sandbox alive, and `sudo` if it is to
serve tasks that declare it. It must not require an engine-specific module search path.
It declares its agent account and whether it starts a desktop, in `ale.*` labels, which
travel with the image through a registry.

**Referencing an upstream image directly is not supported.** Build a curated image from
one instead.

Where a declared capability takes time to become usable, the engine waits for **the
capability itself** — a screenshot that succeeds — rather than for a marker file, which
is true of one image and a lie about the next.

## Consequences

- The keepalive label disappears along with the substitution it existed to control:
  requiring every image to keep itself alive removes both.
- The interpreter hint disappears with it, since there is now one interpreter to use.
- Conformance is checked before a sandbox is used and refused by name. The check is
  partial — whether a command *stays* running cannot be known without running it — and
  says so rather than implying more than it verifies. It starts a container, so its
  result is cached per image.
- Tasks lose the ability to name an upstream image. That is the point: what a task runs on
  becomes a deliberate, inspectable choice rather than whatever a public tag points at
  today.
