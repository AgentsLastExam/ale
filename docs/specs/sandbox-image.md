# Sandbox image specification

What an image must provide to be usable as a sandbox, and how it tells the engine what it
can do.

This exists because the engine used to infer these things, and inference was wrong twice
in ways that looked like something else. It substituted a keep-alive command and replaced
a desktop image's entire graphical session — the image claimed a desktop and had none. It
treated a sandbox as ready when the guest service answered, which is before the screen
exists. **An image declares; the engine reads. It never guesses, and never probes to find
out.**

The rules below are the price of that. They are also the whole of it: an image that
follows them needs no special handling anywhere in the engine.

## Must provide

**One system interpreter**, Python 3.12 or later, on `PATH` as `python3`. The guest
service runs on it, and so do the task's stages and the agent. Anything the guest service
needs — currently `Pillow` and `python-xlib` for in-process screen capture — is installed
into that interpreter **at build time**.

The engine copies `ale_verify` and selected Task Kits into that interpreter's
`site-packages` for the stages that request them; it does not run a package installer.
A task needing a different runtime declares a different image.

**An unprivileged user** with a real home directory at `/home/<user>`, declared as
`ale.user`. The agent runs as this account, and so does the oracle that stands in for it.

The home is derived from that one declaration rather than declared separately. Two facts
that describe the same thing can disagree, and the engine would have no way to tell which
was right; one fact cannot. An agent with root can
change the network policy, the clock and the guest service driving its own sandbox, so a
result obtained that way is not reproducible.

The home directory matters: it *is* the run's workspace. Everything the agent touches
lives under it — the task's declared destinations, its artifacts, the harness's own files
— so nothing has to be granted afterwards and the engine never changes an owner. There is
no separate scratch directory to configure.

**A command that keeps the sandbox alive.** A container lives exactly as long as its
command, and the engine will not supply one. An image with services to start runs them; an
image with nothing to do runs something that simply waits. Either way the choice is the
image's, because substituting one is how a desktop image loses its desktop.

**`sudo`**, if the image is to serve tasks that declare `resources.sudo`. The engine writes
the grant and then confirms it by using it — writing a sudoers rule succeeds in an image
with no `sudo` binary at all, and a task told it had a privilege it never received fails
later, far from the cause.

## Must not require

**An engine-specific module search path.** Shared libraries are installed where the
interpreter already looks. A path only the engine knows is a rule every task author has to
learn, and the one we had set a literal glob — which that variable does not expand, so one
of its two implementations never worked.

**An `ENTRYPOINT`.** It combines with the command in ways that make overriding behaviour
hard to predict. Put what the image does in `CMD`.

## Declares

Labels, in `ale.*`. They are part of the image and travel with it through a registry, so
an image someone else pulls carries the same meaning.

| Label | Meaning | Absent |
|---|---|---|
| `ale.user` | the unprivileged account the agent runs as | `user` |
| `ale.gui` | `"true"` when the image starts a graphical session | no desktop assumed |

`ale.gui` is what tells the engine to wait before treating the sandbox as ready — and it
waits for a screenshot to succeed, not for a marker file, because a marker is true of one
image and a lie about the next.

More will be added as base images multiply: what runtimes are present, whether a GPU is
usable, which task families an image suits. The rule for adding one is that the engine
would otherwise have to guess.

Standard `org.opencontainers.image.*` labels are welcome alongside; they do not collide.

### Disk images

A virtual machine boots a disk, and a disk has nowhere to hang a label. The same
declarations are therefore a file in the guest, `/etc/ale/image.json`, read through the
guest service once the machine is up:

```json
{"user": "user", "gui": false, "port": 7411}
```

`gui` decides one thing: whether the engine waits for a screen before treating the sandbox
as ready. It is not used to admit or refuse a task, and not to decide what tools an agent
is given — both of those questions answer themselves when asked, because a screenshot in a
sandbox without a screen reports that.

Same fields, same meanings, same defaults. `port` is where the guest service listens,
which a container answers instead by being exec'd into.

Everything above applies unchanged, with one substitution: "a command that keeps the
sandbox alive" is what an operating system does by existing, and the guest service is a
service the image enables rather than a process the engine starts.

## The engine guarantees

Given a conforming image, the engine will:

- install the guest service under `/opt/ale`, which is root-owned and root-only, and run
  it as the framework — along with the setup and verify stages and the rewards file, so
  the scorer is out of the agent's reach by permission and not merely by timing;
- create each path a task declared — asset destinations and artifact paths — **as the
  agent**, so no ownership has to be changed afterwards and a path the agent could not
  create fails immediately instead of silently working;
- run the task's setup and verify stages as the framework, and the agent and oracle as the
  agent user;
- install a domain's kits where the interpreter already searches;
- resolve the image to a digest and record it, so a moved tag is detectable;
- wait for a declared capability to actually work before using it.

It will not: alter ownership of anything at all (that is the task's to decide), install
into the interpreter, or substitute the image's command.

## Building one

Derive from an official base image. `sandbox-base-cli` and `sandbox-base-gui` both satisfy
this contract, so an image built `FROM` either inherits it and needs only its own additions.
The disk equivalent is `images/base/qemu/build-desktop.sh`, which starts from Canonical's published
cloud image and adds exactly what this page requires.

Referencing an upstream image directly is not supported. `python:3.12-slim` has no
unprivileged user and no long-lived command; it is a build environment, not a sandbox.
Build a curated image from it instead — which also makes what a task runs on a deliberate
choice rather than whatever that tag points at today.

```dockerfile
FROM ghcr.io/agentslastexam/sandbox-base-cli:0.1.0

# Whatever this domain's tasks need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# The base image's user, command and labels carry over; re-declare only what changes.
```
