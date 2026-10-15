# 0009 - Images are versioned releases and the engine installs the guest service

**Status:** Accepted

## Context

Base images were published under tags that every change overwrote (`latest`, `0.1.0`),
and the Linux bases carried a copy of guestd. One host's Docker daemon serves several
checkouts pinned to different engine commits, and remote builders pull the same tags. A
merged guestd change therefore reached every consumer at its next build: a checkout still
on an older engine got a guest service whose protocol it did not speak, and an existing
Task could build from a different image than the one it was validated on. Content-hash
tags were already immutable but meant nothing to the people choosing them.

## Decision

- Every ALE image is published under the engine's own version, once. The workflow skips a
  version the registry already has, so a changed image ships with the next engine
  version. No `latest` tag is published; an already-published image can be given a
  version by pointing a new tag at the same manifest.
- Tasks may build from any image. Task repositories maintained with ALE name an exact
  base version rather than a floating tag.
- Images carry no guest service. Docker copies the engine's guestd into each sandbox, and
  VM preparation writes it and its systemd unit into each disk, configured from the
  image's `ale.user` and `ale.gui` labels. The guest service identity is part of a VM
  disk's identity, so a guestd change rebuilds the disk, not the base.

## Consequences

An engine update no longer changes what an existing Task builds from or which protocol its
guest speaks; a checkout syncs to a newer engine and newer bases when it chooses to.
Older bases stay usable with newer engines. A changed image takes effect with the next
engine version, and a version under review is built locally until CI publishes it.
